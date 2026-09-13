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

## Three publishing tiers

| Tier | Trigger | Where it goes |
|---|---|---|
| Dev build | push / PR | **CI workflow artifact only** — never published |
| Release rehearsal | **manual** dispatch of `publish-testpypi.yml` on the exact release commit | **TestPyPI** |
| Release | `vX.Y.Z` tag | **PyPI** |

Only the rehearsal and the release upload anything. This policy comes from the
owner directive (2026-09-13), verbatim: *"dev builds are not going to
test.pypi.org, dev builds (if any) can become a artifact of automated
pipeline"* — recorded in [`docs/vox/voice-of-owner.md`](vox/voice-of-owner.md)
and as the `## Update (2026-09-13)` in
[`docs/adr/0011-versioning-and-release-train.md`](adr/0011-versioning-and-release-train.md).

### Dev builds — CI artifacts, never published

`.github/workflows/verify.yml` runs on every `push` and `pull_request`. After
`just verify` it runs `just build` and uploads `dist/` as a workflow artifact
named `dist-dev-<version>` (for example `dist-dev-0.1.1.dev0`), so any CI run's
build is downloadable and identifiable. Nothing on this path uploads: `vX.Y.Z.devN`
is **not** a publishing trigger anywhere.

## One-time setup (per index, per project)

Do this once for each of `cr-core`, `cr-engine`, `cr-providers`, `cr-cli`, and
`clear-record`, before its first upload on that index.

### PyPI pending publisher

On PyPI, a project that does not exist yet is claimed through a **pending
publisher**:

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

### TestPyPI pending publisher

**TestPyPI needs its own pending publisher**, separate from the PyPI one. Do the
same three steps at <https://test.pypi.org> with the rehearsal workflow identity:

| Field | Value |
|---|---|
| PyPI project name | `cr-core` (then `cr-engine`, `cr-providers`, `cr-cli`, `clear-record`) |
| Owner | `zhaoweny` |
| Repository name | `clear-record` |
| Workflow name | `publish-testpypi.yml` |
| Environment name | `testpypi` |

### GitHub environments

The GitHub **environments** are the second half of each gate: in the repo's
**Settings → Environments**, create `pypi` and `testpypi` and require reviewers,
so every upload waits for a human approval. The PyPI upload uses `publish.yml`
+ the `pypi` environment; the TestPyPI rehearsal uses `publish-testpypi.yml` +
the `testpypi` environment.

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
just bump-dev                # 0.1.1.dev0 -> 0.1.1.dev1 (a new dev series)
just set-version 0.1.1       # drop the suffix (release)
```

After any bump, regenerate and commit the lockfile — `just verify` runs
`uv sync --locked` and fails on a stale lock:

```sh
uv lock
```

The publishable version shape is `X.Y.Z`. `X.Y.Z.devN` is the **in-development
marker** `main` carries, not a publishable version: a rehearsal or release first
runs `just set-version X.Y.Z`. **No local versions** (`+g<sha>`): PyPI rejects
them, so even a dev version stops at `.devN`.

## Release rehearsal loop (TestPyPI)

The rehearsal qualifies the **exact commit** that will become `vX.Y.Z`. TestPyPI
is a **separate index**, so publishing `0.1.1` there does **not** consume
`0.1.1` on PyPI (a version on PyPI can never be reused; on TestPyPI it is only a
rehearsal). TestPyPI may also be **pruned**, so it is not an archive.

1. Set the release version and relock:

   ```sh
   just set-version 0.1.1
   uv lock
   just verify
   ```

2. Commit that exact version — **do not tag yet**:

   ```sh
   git add -A && git commit -m "release: v0.1.1"
   ```

3. Dispatch `publish-testpypi.yml` **on that commit** (GitHub → Actions →
   *publish-testpypi* → **Run workflow**, selecting the branch whose tip is that
   commit — or `gh workflow run publish-testpypi.yml --ref <branch>`), then
   approve the `testpypi` environment when prompted.

4. Inspect all five projects on TestPyPI at the just-published version:
   <https://test.pypi.org/project/cr-core/> (then `cr-engine`, `cr-providers`,
   `cr-cli`, `clear-record`). Each should show a **wheel and an sdist** with the
   expected metadata (exact `==` sibling pins, bundled license files).

5. Run the external resolver smoke below against the rehearsal.

### External resolver smoke (TestPyPI)

Prove a fresh environment can resolve and run the published graph. TestPyPI
hosts only the five dists, so it must be an **explicit** index (third-party
dependencies keep coming from PyPI):

```sh
# A disposable uv project. TestPyPI is separate from PyPI, so the five dists
# are pinned to an explicit TestPyPI index; numpy/soundfile and friends resolve
# from PyPI as usual.
mkdir -p /tmp/cr-testpypi-smoke && cd /tmp/cr-testpypi-smoke
cat > pyproject.toml <<'TOML'
[project]
name = "cr-testpypi-smoke"
version = "0"
requires-python = ">=3.12"
dependencies = [
  "clear-record==0.1.1",
  "cr-cli==0.1.1",
  "cr-core==0.1.1",
  "cr-engine==0.1.1",
  "cr-providers==0.1.1",
]

[tool.uv]
package = false

[[tool.uv.index]]
name = "testpypi"
url = "https://test.pypi.org/simple"
explicit = true

[tool.uv.sources]
clear-record = { index = "testpypi" }
cr-cli = { index = "testpypi" }
cr-core = { index = "testpypi" }
cr-engine = { index = "testpypi" }
cr-providers = { index = "testpypi" }
TOML

uv sync
uv run clear-record --help
uv run clear-record backends
uv run python -c "import cr_core, cr_engine, cr_providers, cr_cli, clear_record"
```

All three checks must succeed. `clear-record --help` proves the console
script; `backends` exercises a real command; the import check proves all five
modules load. Delete `/tmp/cr-testpypi-smoke` afterward.

## Release loop (PyPI)

A release is "drop the dev suffix and tag the rehearsed commit":

1. **Rehearse first** (above): the exact commit was published and smoke-tested
   on TestPyPI.

2. Tag and push that same commit: `git tag v0.1.1 && git push origin v0.1.1`.

3. Create the matching **GitHub Release**.

4. `publish.yml` builds and publishes all five members to **PyPI**. Approve the
   `pypi` environment when prompted.

Both publishing workflows' `build` jobs run `just verify` + `just build`, then
smoke-install the just-built facade into a clean venv and run
`clear-record --help`. `just build` passes `--no-sources`, so the published
graph is proven to resolve without `tool.uv.sources`. Every wheel and sdist
bundles the MIT `LICENSE` (`license-files = ["LICENSE"]`).

### Sanity-check the built metadata

The release commit is on `main`; the tag points at it. After a build you can
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
`main` moves to 0.2 development (ADR-0011).

## First upload: clean history

`v0.1.0` was a **source-only OSS tag** — nothing was ever uploaded for it. The
first PyPI release is **`v0.1.1`**, tag-driven: land the release commit, rehearse
it on TestPyPI (`publish-testpypi.yml`, above), then
`git tag v0.1.1 && git push origin v0.1.1`. No special first-run incantation is
needed.

`workflow_dispatch` remains on `publish.yml` only as an **emergency hatch** (the
TestPyPI rehearsal is a separate, intentional manual workflow). The publish job
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
