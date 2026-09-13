# ADR-0011 — Versioning and the release train

Status: active (amended by the 2026-09-13 Update below)
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
  carries a git hash cannot be uploaded. `X.Y.Z.devN` is the furthest a
  publishable snapshot can go.
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
- Revisit when 0.2 development starts (cut `releases/v0.1.x`) or if the
  publishable-version shape ever needs more than `X.Y.Z[.devN]`.

## Update (2026-09-13) — dev builds are CI artifacts; TestPyPI is a manual rehearsal

Owner (2026-09-13, verbatim): *"dev builds are not going to test.pypi.org, dev
builds (if any) can become a artifact of automated pipeline"*. This
**supersedes** the snapshot-to-TestPyPI decision in the Decision section and the
"TestPyPI first. Snapshots are pre-releases…" bullet in the Rationale section;
that text is kept for the record. The bullets below are agent-authored framing,
not owner quotes: bullet 1 records the owner's decision, bullets 2–4 are
`[DESIGN]` choices for implementing it.

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
- [DESIGN] **PyPI stays release-tag only.** `vX.Y.Z` triggers `publish.yml`
  with the unchanged filters (`"v[0-9]*"` + `"!v[0-9]*.dev*"`) and the
  tag↔manifest guard. The `vX.Y.Z.devN` form is no longer a publishing trigger
  anywhere; `main` may still carry `X.Y.Z.devN` as the work-in-progress version.

The three tiers are therefore:

| Tier | Trigger | Where it goes |
| --- | --- | --- |
| Dev build | push / PR | **CI workflow artifact only** — never published |
| Release rehearsal | **manual** dispatch, on the exact release commit | **TestPyPI** |
| Release | `vX.Y.Z` tag | **PyPI** |

The static version literal, `scripts/bump-version.py`, the lockstep manifests
and the exact `==` pins are unchanged — only the publishing policy changed. See
[`docs/releasing.md`](../releasing.md) and
[`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).
