# ADR-0011 — Versioning and the release train

Status: active (amended by the 2026-09-13 Updates below)
Date: 2026-09-13

## Context

- [VOICE: owner] 2026-09-13: called for a work-in-progress `-dev` build tag
  and a `releases/v0.1.x` release train whose current `main` is that train;
  verbatim in [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md). The
  three owner-selected specifics (option wording agent-authored) are recorded
  there as `[DECISION]` items.
- [FACT] `uv_build` (ADR-0010) requires a **static** `version` in `[project]`;
  `dynamic = ["version"]` is a hard error. A version cannot be derived from git
  or the tag at build time, so the version literal must live in the manifests.
- [FACT] `uv version --package X --bump dev` bumps only that project. It does
  **not** rewrite the sibling members' exact `==` pins, and the workspace root
  is a separate (virtual) project. A workspace bump therefore has to touch
  **six** manifests — the root plus `cr-core`, `cr-engine`, `cr-providers`,
  `cr-cli`, `clear-record` — and their pins together.
- [FACT] PyPI rejects **local versions** (`0.1.1.dev0+g<sha>`); a version that
  carries a git hash cannot be uploaded. The `.devN` form is a CI-artifact
  marker and is never published; the publishable pre-release is `X.Y.ZrcN`.
- [FACT] The intra-project dependencies are exact-pinned and released in
  lockstep (ADR-0009), so a half-released set cannot resolve; the version is a
  release-train property, not a per-package one.

## Decision

- [DECISION] The version is **static** and identical across the six manifests.
  `main` carries **`X.Y.Z.devN`** (today `0.1.1.dev0`); the dev segment is
  bumped for each snapshot. There is no git-derived version (`uv_build`
  forbids it).
- [DECISION] A single stdlib-only script, `scripts/bump-version.py`, owns the
  bump across all six manifests. The current version literal is replaced
  verbatim in every manifest, so both `version = "…"` and every `==` pin move
  together and formatting is preserved; nothing is parsed as TOML. It exposes
  `--show` (print; abort if the six disagree), `--dev` (`0.1.1` →
  `0.1.1.dev0`; `.devN` → `.devN+1`) and an explicit `X.Y.Z[.devN]`. `just`
  wraps it as `version`, `bump-dev` and `set-version VERSION`.
- [DECISION] **Snapshots** are tagged `vX.Y.Z.devN`; the tag triggers
  `.github/workflows/publish-dev.yml` (deleted 2026-09-13 — see the Update
  below), which published all five members to **TestPyPI** via OIDC (the
  `testpypi` environment). TestPyPI needs its **own** pending publisher,
  separate from the PyPI one.
- [DECISION] **A release** is "drop the dev suffix": `just set-version X.Y.Z`,
  commit, tag `vX.Y.Z`. That tag triggers `.github/workflows/publish.yml`, which
  publishes to **PyPI**. Dev tags are excluded from the PyPI filter
  (`"v[0-9]*"` + `"!v[0-9]*.dev*"`), and the publish job refuses to run unless
  the checked-out ref is the tag matching the manifest version.
- [DECISION] The release train is **lazy**: `main` is the `releases/v0.1.x`
  train while 0.1 is the only supported line. The `releases/v0.1.x` branch is
  cut when 0.2 development starts, so no maintenance branch exists before it
  has work to carry.
- [DECISION] **No local versions.** A snapshot stops at `X.Y.Z.devN` because
  PyPI rejects `+g<sha>`; CI never constructs a version that cannot be
  published.

## Rationale

- **One literal, six files.** Pins and `version` are the same string; replacing
  the old literal cannot miss a pin and cannot reformat a manifest, where TOML
  round-tripping would.
- **`uv_build` needs statics, so a script owns the bump.** Since the backend
  cannot derive a version and `uv version` cannot move the pins, a deterministic
  workspace-wide bump is the only way to keep lockstep.
- **TestPyPI first.** Snapshots are pre-releases; publishing them to TestPyPI
  exercises the real index/CDN/trusted-publishing path without claiming
  versions on PyPI (which can never be reused — ADR-0009).
- **Lazy branch.** A maintenance branch with no divergent commits is overhead;
  cut it when 0.2 work actually needs a 0.1 line to keep moving.

## Discarded alternatives

- **`dynamic = ["version"]` / git-derived version** — hard error under
  `uv_build`; cannot be used.
- **`uv version --bump dev` alone** — moves one project and leaves the sibling
  `==` pins stale; the next resolve fails.
- **Local/dev versions with `+g<sha>`** — PyPI rejects them; a snapshot would
  build but never publish.
- **Publish snapshots to PyPI** — burns immutable version numbers on
  pre-releases and mixes unfinished builds into the public resolver.
- **Eager `releases/v0.1.x` branch** — a branch with nothing to carry until 0.2
  development starts.

## Consequences / review hook

- **`uv.lock` must follow the bump.** After `just bump-dev` / `set-version`, run
  `uv lock` and commit it; `just verify` (`uv sync --locked`) fails on a stale
  lock, which is the guard rail.
- **`bump-version.py` is the version source of truth.** It discovers the root
  plus `packages/*/pyproject.toml`, so a new workspace member is picked up by
  the lockstep check with no list edit.
- Revisit when 0.2 development starts (cut `releases/v0.1.x`) or if the version
  shapes the manifests carry ever need more than `X.Y.Z[{a|b|rc}N][.devN]`.

## Update (2026-09-13) — dev builds are CI artifacts; TestPyPI is a manual rehearsal

Owner (2026-09-13, verbatim): *"dev builds are not going to test.pypi.org, dev
builds (if any) can become a artifact of automated pipeline"*. This
**supersedes** the snapshot-to-TestPyPI decision in the Decision section and the
"TestPyPI first. Snapshots are pre-releases…" bullet in the Rationale section;
that text is kept for the record. The first four bullets below are
agent-authored framing, not owner quotes: bullet 1 records the owner's decision,
bullets 2–4 are `[DESIGN]` choices for implementing it. (The release-candidate
policy added later the same day is a separate Update below, so this section
stays readable as the dev-build record.)

- [DECISION] **Dev builds never publish anywhere.** Every `push`/`pull_request`
  build is uploaded as a CI workflow artifact by `verify.yml` (named with the
  manifest version); no `v*.dev*` version reaches TestPyPI or PyPI.
- [DESIGN] The automatic `v*.dev*` → TestPyPI snapshot workflow
  (`publish-dev.yml`) is **deleted**. TestPyPI is now a **manual release
  rehearsal** only: `publish-testpypi.yml`, `on: workflow_dispatch`, dispatched
  by hand on the **exact commit** that will become the release tag. Because
  TestPyPI is a separate index, rehearsing `X.Y.Z` there does not consume
  `X.Y.Z` on PyPI.
- [DESIGN] TestPyPI keeps its **own** pending publishers, separate from
  PyPI's: workflow `publish-testpypi.yml`, environment `testpypi`.
- [DESIGN] **PyPI stays tag-triggered.** `vX.Y.Z` triggers `publish.yml` with
  the unchanged filters (`"v[0-9]*"` + `"!v[0-9]*.dev*"`) and the tag↔manifest
  guard. The `vX.Y.Z.devN` form is no longer a publishing trigger anywhere;
  `main` may still carry `X.Y.Z.devN` as the work-in-progress version.

## Update (2026-09-13) — release candidates publish to PyPI

Owner (2026-09-13, verbatim): *"let's make a change. the rc build could go
test-pypi; real pypi sees stable releases"* — recorded in
[`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md). Its "real pypi sees
stable releases" clause was superseded later the same day, when the owner
**accepted** publishing release candidates to PyPI as well. That acceptance is
hedged (*"I think I'd accept"*), not a firm directive; the verbatim is recorded
in [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).

- [DECISION] **A release candidate (`X.Y.ZrcN`) is rehearsed on TestPyPI and
  then published to PyPI.** The `publish-testpypi.yml` lane stays manual
  (`workflow_dispatch`) and refuses `.devN`; it carries both `X.Y.ZrcN` and the
  final stable `X.Y.Z` rehearsal.
- [DECISION, owner-accepted] **PyPI receives stable releases *and* release
  candidates.** The earlier "PyPI receives stable releases only" rule is
  superseded: a `vX.Y.ZrcN` tag publishes the candidate to PyPI as a PEP 440
  pre-release, and `vX.Y.Z` publishes the stable release. PyPI is **publishable
  as an `rc`, never as a dev version**; dev builds publish to neither index.
  (Owner acceptance above, not an unqualified directive.)
- [DESIGN] The mechanics of the decisions above: `scripts/bump-version.py`
  accepts `X.Y.Z[{a|b|rc}N][.devN]` and gains `--rc` (`X.Y.Z` → `X.Y.Zrc1`;
  `X.Y.ZrcN` → `X.Y.Zrc{N+1}`; an accepted-but-unused `a`/`b` advances to
  `rc1`), wrapped as `just bump-rc`; `publish.yml` excludes only dev tags
  (`"v[0-9]*"`, `"!v[0-9]*.dev*"`), and its publish job refuses a manifest
  version carrying `.devN`, so stable and `rc` tags both publish while a dev
  build can never reach PyPI — not even via a manual `workflow_dispatch`. The
  version literal, the lockstep manifests and the exact `==` pins stay static;
  what changed is the accepted shapes (`--rc`), the tag filter, and the policy.

The tiers are therefore:

| Tier | Version | Trigger | Where it goes |
| --- | --- | --- | --- |
| Dev build | `X.Y.Z.devN` | push / PR | **CI workflow artifact only** — never published |
| Release candidate | `X.Y.ZrcN` | **manual** dispatch of `publish-testpypi.yml`, then tag `vX.Y.ZrcN` | **TestPyPI** (rehearsal), then **PyPI** (pre-release) |
| Stable release | `X.Y.Z` | **manual** rehearsal on the exact release commit, then tag `vX.Y.Z` | **PyPI** |

A PyPI pre-release is opt-in for installers: `pip`/`uv` select `X.Y.ZrcN` only
with `--pre`, an explicit pin, or when no stable version satisfies the range.

See [`docs/releasing.md`](../releasing.md) and
[`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).
