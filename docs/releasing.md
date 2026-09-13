# Releasing clear-record

The workspace publishes **one** PyPI distribution, `clear-record`:

| Distribution | Import | Contents |
|---|---|---|
| `clear-record` | `clear_record` | the whole tool: `clear_record.{core,engine,providers,cli}` layers as subpackages |

Each release moves both `version =` fields **together** — the virtual root and
the single member — and there are no intra-project dependencies left to pin
(ADR-0012). There are **no PyPI tokens or repository secrets**: publishing uses
OIDC trusted publishing
([`docs/adr/0009-packaging-and-distribution.md`](adr/0009-packaging-and-distribution.md));
the train and versioning are
[`docs/adr/0011-versioning-and-release-train.md`](adr/0011-versioning-and-release-train.md).

`clear-record` is the **sole owner** of the `clear-record` console script
(`clear_record.cli:main`), so `uvx clear-record` resolves and runs it without the
dependency-provided-command warning. The four former `cr-*` members are now
internal `clear_record` subpackages (ADR-0012); the release machinery that used
to coordinate them is being simplified in a follow-up slice, so the workflow
files still build the workspace and smoke-install the command.

## Three publishing tiers

| Tier | Version | Trigger | Where it goes |
|---|---|---|---|
| Dev build | `X.Y.Z.devN` | push / PR | **CI workflow artifact only** — never published |
| Release candidate | `X.Y.ZrcN` | **manual** dispatch of `publish-testpypi.yml`, then tag `vX.Y.ZrcN` | **TestPyPI** (rehearsal), then **PyPI** (as a PEP 440 pre-release) |
| Stable release | `X.Y.Z` | `vX.Y.Z` tag | **PyPI** |

Only the TestPyPI and PyPI lanes upload anything, and both are gated by a human
approval. Dev builds never publish; TestPyPI rehearses the release candidate
before it goes to PyPI, and PyPI receives both the candidate and the stable
release. This policy comes from the owner (2026-09-13): dev builds are CI
artifacts, not TestPyPI uploads, and the owner accepted publishing release
candidates to PyPI as pre-releases. The owner's verbatim wording — including
that later acceptance, which superseded "real pypi sees stable releases" — is
recorded in [`docs/vox/voice-of-owner.md`](vox/voice-of-owner.md), and the
policy as the `## Update (2026-09-13)` sections in
[`docs/adr/0011-versioning-and-release-train.md`](adr/0011-versioning-and-release-train.md).

PyPI pre-releases are **opt-in for installers**: `pip`/`uv` only select
`X.Y.ZrcN` when told to allow pre-releases (`--pre`), when the requirement pins
it explicitly (for example `clear-record==0.1.1rc1`), or when no stable version
satisfies the range. A bare `pip install clear-record` therefore keeps getting
the newest stable release.

### Dev builds — CI artifacts, never published

`.github/workflows/verify.yml` runs on every `push` and `pull_request`. After
`just verify` it runs `just build` and uploads `dist/` as a workflow artifact
named `dist-<version>` (for example `dist-0.1.1.dev0`; an rc version ships
through this path as `dist-0.1.1rc1` too), so any CI run's build is downloadable
and identifiable. Nothing on this path uploads: `vX.Y.Z.devN` is **not** a
publishing trigger anywhere.

## One-time setup (per index)

Do this once for **`clear-record`**, before its first upload on that index.

### PyPI pending publisher

On PyPI, a project that does not exist yet is claimed through a **pending
publisher**:

1. Sign in to <https://pypi.org> and open **Your account → Publishing →
   Add a pending publisher → GitHub**.
2. Fill in, exactly:

   | Field | Value |
   |---|---|
   | PyPI project name | `clear-record` |
   | Owner | `zhaoweny` |
   | Repository name | `clear-record` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. That is the only project to claim on this index.

### TestPyPI pending publisher

**TestPyPI needs its own pending publisher**, separate from the PyPI one. Do the
same three steps at <https://test.pypi.org> with the rehearsal workflow identity:

| Field | Value |
|---|---|
| PyPI project name | `clear-record` |
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

> The package name is settled: `clear-record` is the single published dist
> (ADR-0012), and its four layers are internal subpackages. Claim one pending
> publisher per index.

## Versioning: one dev series on `main`

The root and the single member carry one **static** version (ADR-0011) —
`uv_build` forbids `dynamic = ["version"]`, so a script owns the bump.
`scripts/bump-version.py` (wrapped by `just`) replaces the exact old literal in
both manifests at once:

```sh
just version                 # print the current version (aborts if the two disagree)
just bump-dev                # 0.1.1.dev0 -> 0.1.1.dev1 (a new dev series)
just bump-rc                 # 0.1.1.dev0 or 0.1.1 -> 0.1.1rc1; 0.1.1rcN -> 0.1.1rc{N+1}
just set-version 0.1.1       # drop the suffix (stable release)
```

After any bump, regenerate and commit the lockfile — `just verify` runs
`uv sync --locked` and fails on a stale lock:

```sh
uv lock
```

The version shapes the manifests carry are `X.Y.Z` (**stable**) and `X.Y.ZrcN`
(**release candidate**) — both go to PyPI, the candidate after its TestPyPI
rehearsal. `X.Y.Z.devN` is the **in-development marker** `main` carries and is
never published; `a`/`b` are accepted by the validator but unused, and `--rc`
advances an `a`/`b` to `rc1`. To move off the dev marker, cut an `rc` with `just
bump-rc`, or go straight to stable with `just set-version X.Y.Z`. **No local
versions** (`+g<sha>`): PyPI rejects them, so even a dev version stops at
`.devN`.

## Release rehearsal loop (TestPyPI)

This lane qualifies the **exact commit** that will become the release tag
(`vX.Y.ZrcN` for a candidate, `vX.Y.Z` for the stable release), and it carries
both **release candidates** (`X.Y.ZrcN`) and the **final stable rehearsal**
(`X.Y.Z`). It is manual (`workflow_dispatch`) and refuses a `.devN`
version; there is no tag trigger. TestPyPI is a **separate index**, so
publishing `0.1.1` there does **not** consume `0.1.1` on PyPI (a version on PyPI
can never be reused; on TestPyPI it is only a rehearsal). TestPyPI may also be
**pruned**, so it is not an archive.

1. Set the version and relock. To cut a release candidate, roll the rc segment:

   ```sh
   just bump-rc      # 0.1.1.dev0 -> 0.1.1rc1 (then 0.1.1rc1 -> 0.1.1rc2, ...)
   uv lock
   just verify
   ```

   To rehearse the final stable release, drop the suffix instead:

   ```sh
   just set-version 0.1.1
   uv lock
   just verify
   ```

2. Commit that exact version — **do not tag yet**:

   ```sh
   git add -A && git commit -m "release: v0.1.1rc1"   # or "release: v0.1.1"
   ```

3. Dispatch `publish-testpypi.yml` **on that commit** (GitHub → Actions →
   *publish-testpypi* → **Run workflow**, selecting the branch whose tip is that
   commit — or `gh workflow run publish-testpypi.yml --ref <branch>`), then
   approve the `testpypi` environment when prompted.

4. Inspect `clear-record` on TestPyPI at the just-published version:
   <https://test.pypi.org/project/clear-record/>. It should show a **wheel and an
   sdist** with the expected metadata (bundled license file, no `cr-*`
   requirements).

5. Run the external resolver smoke below against the rehearsal (adjust the pins
   to the version you published).

### External resolver smoke (TestPyPI)

Prove a fresh environment can resolve and run the published distribution.
TestPyPI hosts only `clear-record`, so it must be an **explicit** index
(third-party dependencies keep coming from PyPI):

```sh
# A disposable uv project. TestPyPI is separate from PyPI, so the dist is
# pinned to an explicit TestPyPI index; numpy/soundfile and friends resolve
# from PyPI as usual.
mkdir -p /tmp/cr-testpypi-smoke && cd /tmp/cr-testpypi-smoke
cat > pyproject.toml <<'TOML'
[project]
name = "cr-testpypi-smoke"
version = "0"
requires-python = ">=3.12"
dependencies = [
  "clear-record==0.1.1",
]

[tool.uv]
package = false

[[tool.uv.index]]
name = "testpypi"
url = "https://test.pypi.org/simple"
explicit = true

[tool.uv.sources]
clear-record = { index = "testpypi" }
TOML

uv sync
uv run clear-record --help
uv run clear-record backends
uv run python -c "import clear_record, clear_record.core, clear_record.engine, clear_record.providers, clear_record.cli"
```

All three checks must succeed. `clear-record --help` proves the console script;
`backends` exercises a real command; the import check proves every layer loads.
Delete `/tmp/cr-testpypi-smoke` afterward.

## Release loops

A **release candidate** publishes to PyPI by tagging `vX.Y.ZrcN`, and a
**stable release** by tagging `vX.Y.Z`. Both are tag-driven: dev tags are
excluded from `publish.yml` by its tag filter, and its publish job refuses a
manifest version carrying `.devN`, so a dev build can never reach PyPI — not
even via a manual dispatch. A PyPI pre-release is **opt-in for installers**
(see the tiers above): it is published, but not installed by default.

### Release candidate loop

1. **Rehearse first** (above): the `X.Y.ZrcN` commit was published and
   smoke-tested on TestPyPI.

2. Tag and push that same commit: `git tag v0.1.1rc1 && git push origin
   v0.1.1rc1`.

3. Create the matching **GitHub Release**, marked as a **pre-release**.

4. `publish.yml` builds and publishes the single `clear-record` dist to **PyPI** as a PEP 440
   pre-release. Approve the `pypi` environment when prompted.

5. To advance the candidate, `just bump-rc` (for example `rc1` → `rc2`), relock,
   commit, rehearse on TestPyPI again, and tag `vX.Y.Zrc2`. A PyPI version can
   never be reused, so a bad candidate is superseded, never re-tagged.

### Stable release loop

1. **Rehearse first** (above): the exact commit was published and smoke-tested
   on TestPyPI. If the last candidate was an rc (`0.1.1rcN`), drop the suffix so
   the manifest is stable, relock and commit: `just set-version 0.1.1 &&
   uv lock`.

2. Tag and push that same commit: `git tag v0.1.1 && git push origin v0.1.1`.

3. Create the matching **GitHub Release**.

4. `publish.yml` builds and publishes the single `clear-record` dist to **PyPI**. Approve the
   `pypi` environment when prompted.

Both publishing workflows' `build` jobs run `just verify` + `just build`, then
smoke-install the just-built dist into a clean venv and run
`clear-record --help`. `just build` passes `--no-sources`, so the published
graph is proven to resolve without `tool.uv.sources`. The wheel and sdist bundle
the MIT `LICENSE` (`license-files = ["LICENSE"]`).

### Sanity-check the built metadata

The release commit is on `main`; the tag points at it. After a build you can
read the wheel back locally:

```sh
just build
version="$(just version)"
unzip -p dist/clear_record-${version}-py3-none-any.whl '*/METADATA' | grep -E 'Requires-Dist|Provides-Extra|License-File'
```

You should see `Requires-Dist: numpy>=2.0`, `Requires-Dist: soundfile>=0.12`,
the four backend extras (`Provides-Extra`) and `License-File: LICENSE` — and
**no `cr-*` requirement**. The wheel also contains the four layers and the
bundled license, and the console script targets the CLI layer:

```sh
unzip -l dist/clear_record-${version}-py3-none-any.whl | grep -E 'clear_record/(core|engine|providers|cli)/|licenses/LICENSE'
unzip -p dist/clear_record-${version}-py3-none-any.whl '*/entry_points.txt'
```

You should see the `clear_record/{core,engine,providers,cli}` packages,
`dist-info/licenses/LICENSE`, and `clear-record = clear_record.cli:main`.

## The release train is lazy

`main` **is** the `releases/v0.1.x` train while 0.1 is the only supported line.
There is no maintenance branch until 0.2 development starts; at that point cut
`releases/v0.1.x` from the last 0.1 tag so 0.1 patch fixes have a home while
`main` moves to 0.2 development (ADR-0011).

## First upload: clean history

`v0.1.0` was a **source-only OSS tag** — nothing was ever uploaded for it. The
first **stable** PyPI release is **`v0.1.1`**, tag-driven: land the release
commit, rehearse it on TestPyPI (`publish-testpypi.yml`, above), then
`git tag v0.1.1 && git push origin v0.1.1`. No special first-run incantation is
needed.

`workflow_dispatch` remains on `publish.yml` only as an **emergency hatch** (the
TestPyPI rehearsal is a separate, intentional manual workflow). The publish job
refuses to run unless the manifest is publishable (a stable `X.Y.Z` or a
candidate `X.Y.ZrcN`, but never a dev version) and the checked-out ref is a tag
whose `v<manifest version>` matches the manifests, so neither a dev build nor a
dispatch from an arbitrary `main` commit can publish a mismatched artifact.

## Version immutability

**A PyPI version can never be reused or overwritten.** Uploading files that
already exist for a version fails; deleting and re-uploading the same version
number is not allowed. If a release is bad:

- publish a **new patch version** (`X.Y.(Z+1)`) with the fix, and
- optionally **yank** the bad version on PyPI (it disappears from resolution but
  stays available to anyone who already pinned it) — yank, don't delete.

Never re-tag or re-run a publish for a version that already went out.
