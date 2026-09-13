# Releasing clear-record

The five workspace members are published as five PyPI distributions in lockstep:

| Distribution | Import | Contents |
|---|---|---|
| `cr-core` | `cr_core` | backend-agnostic domain model (no third-party deps) |
| `cr-engine` | `cr_engine` | audio I/O, alignment, reconcile (numpy, soundfile) |
| `cr-providers` | `cr_providers` | per-vendor ASR adapters (optional extras) |
| `cr-cli` | `cr_cli` | CLI implementation package; declares no console script |
| `clear-record` | `clear_record` | facade over `cr-cli`; the public install name (a `clear_record` module) |

Each release moves all six `version =` fields **together** — the virtual root
plus the five members — and every intra-project dependency is exact-pinned
(`cr-engine==X.Y.Z`), so a half-released set cannot resolve. There are **no PyPI
tokens or repository secrets**: publishing uses OIDC trusted publishing
([`docs/adr/0009-packaging-and-distribution.md`](adr/0009-packaging-and-distribution.md));
the train and versioning are
[`docs/adr/0011-versioning-and-release-train.md`](adr/0011-versioning-and-release-train.md).

`clear-record` is a **facade**: it ships a small `clear_record` module
(`clear_record.main` re-exports `cr_cli.cli.main`) and is the **sole owner** of
the `clear-record` console script, so `uvx clear-record` resolves and runs it
without the dependency-provided-command warning. `cr-cli` is the CLI
implementation package and declares **no console script**, so `uvx cr-cli`
provides no command by design; the public install name is `clear-record`. The
facade depends on `cr-cli==X.Y.Z`, so the two belong to the same release.

## One-time setup (per PyPI project)

Do this once for each of `cr-core`, `cr-engine`, `cr-providers`, `cr-cli`, and
`clear-record`, before its first upload on that index. On PyPI, a project that
does not exist yet is claimed through a **pending publisher**:

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

**TestPyPI needs its own pending publisher**, separate from the PyPI one. Do the
same three steps at <https://test.pypi.org> with the snapshot workflow identity:

| Field | Value |
|---|---|
| PyPI project name | `cr-core` (then `cr-engine`, `cr-providers`, `cr-cli`, `clear-record`) |
| Owner | `zhaoweny` |
| Repository name | `clear-record` |
| Workflow name | `publish-dev.yml` |
| Environment name | `testpypi` |

The GitHub **environments** are the second half of each gate: in the repo's
**Settings → Environments**, create `pypi` and `testpypi` and require reviewers,
so every publish waits for a human approval.

> The CLI dist name is settled: `cr-cli` is the implementation package and
> `clear-record` is the public install name (a facade over `cr-cli`; ADR-0009,
> owner direction 2026-09-13). Claim **both** pending publishers on each index.

## Versioning: one dev series on `main`

The six manifests carry one **static** version (ADR-0011) — `uv_build` forbids
`dynamic = ["version"]`, and `uv version --bump dev` does not rewrite the
sibling `==` pins, so a script owns the bump. `scripts/bump-version.py` (wrapped
by `just`) replaces the exact old literal in all six manifests at once:

```sh
just version                 # print the current version (aborts if the six disagree)
just bump-dev                # 0.1.1.dev0 -> 0.1.1.dev1 (snapshot)
just set-version 0.1.1       # drop the suffix (release)
```

After any bump, regenerate and commit the lockfile — `just verify` runs
`uv sync --locked` and fails on a stale lock:

```sh
uv lock
```

The publishable version shape is `X.Y.Z` or `X.Y.Z.devN`. **No local versions**
(`+g<sha>`): PyPI rejects them, so a snapshot stops at `.devN`.

## Snapshot loop (TestPyPI)

Snapshots are pre-releases for testing the install path; they never reach PyPI.

1. Bump the dev segment and relock:

   ```sh
   just bump-dev
   uv lock
   ```

2. Commit both and tag: `git add -A && git commit -m "chore(release): snapshot"`
   then `git tag v0.1.1.dev1 && git push origin v0.1.1.dev1`.

3. `publish-dev.yml` builds, smoke-installs the facade, and publishes all five
   members to **TestPyPI**. Approve the `testpypi` environment when prompted.

## Release loop (PyPI)

A release is "drop the dev suffix and tag it":

1. Set the release version and relock:

   ```sh
   just set-version 0.1.1
   uv lock
   just verify
   ```

2. Commit, tag, and push: `git commit -am "release: v0.1.1"` then
   `git tag v0.1.1 && git push origin v0.1.1`.

3. Create the matching **GitHub Release**.

4. `publish.yml` builds and publishes all five members to **PyPI**. Approve the
   `pypi` environment when prompted.

The `build` job in both workflows runs `just verify` + `just build`, then
smoke-installs the just-built facade into a clean venv and runs
`clear-record --help`. `just build` passes `--no-sources`, so the published
graph is proven to resolve without `tool.uv.sources`. Every wheel and sdist
bundles the MIT `LICENSE` (`license-files = ["LICENSE"]`).

### Sanity-check the built metadata

The bump commit is on `main`; the tag points at it. After a build you can
read the pins back locally:

```sh
just build
version="$(just version)"
unzip -p dist/cr_cli-${version}-py3-none-any.whl '*/METADATA' | grep Requires-Dist
```

You should see `cr-core==X.Y.Z`, `cr-engine==X.Y.Z`, `cr-providers==X.Y.Z`.

The facade wheel should show the `cr-cli` pin, the `clear_record` module, the
console script, and the bundled license:

```sh
unzip -l dist/clear_record-${version}-py3-none-any.whl | grep -E 'clear_record/__init__.py|licenses/LICENSE'
unzip -p dist/clear_record-${version}-py3-none-any.whl '*/METADATA' | grep -E 'Requires-Dist|Provides-Extra|License-File'
unzip -p dist/clear_record-${version}-py3-none-any.whl '*/entry_points.txt'
```

You should see `clear_record/__init__.py`, `dist-info/licenses/LICENSE`,
`Requires-Dist: cr-cli==X.Y.Z`, the backend extras, `License-File: LICENSE`, and
`clear-record = clear_record:main`.

## The release train is lazy

`main` **is** the `releases/v0.1.x` train while 0.1 is the only supported line.
There is no maintenance branch until 0.2 development starts; at that point cut
`releases/v0.1.x` from the last 0.1 tag so 0.1 patch fixes have a home while
`main` moves to 0.2 snapshots (ADR-0011).

## First upload: clean history

`v0.1.0` was a **source-only OSS tag** — nothing was ever uploaded for it. The
first PyPI release is **`v0.1.1`**, tag-driven: land the release commit, then
`git tag v0.1.1 && git push origin v0.1.1`. No special first-run incantation is
needed.

`workflow_dispatch` remains only as an **emergency hatch**. The publish job
refuses to run unless the checked-out ref is a tag whose `v<manifest version>`
matches the manifests, so a dispatch from an arbitrary `main` commit cannot
publish a mismatched artifact.

## Version immutability

**A PyPI version can never be reused or overwritten.** Uploading files that
already exist for a version fails; deleting and re-uploading the same version
number is not allowed. If a release is bad:

- publish a **new patch version** (`X.Y.(Z+1)`) with the fix, and
- optionally **yank** the bad version on PyPI (it disappears from resolution but
  stays available to anyone who already pinned it) — yank, don't delete.

Never re-tag or re-run a publish for a version that already went out.
