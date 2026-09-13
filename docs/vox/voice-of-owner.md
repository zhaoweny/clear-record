# Voice of Owner — clear-record

The authoritative recorded owner intent. A `VOICE` entry in
`docs/architecture.md` or an ADR should trace back to a line here (or to a dated
ADR). These are the owner's own words/positions, distilled from the original
concept (see `docs/architecture.md` §9).

## Project framing

- The original concept was **"from many recordings to one clear record"** — a
  post-processing system that ingests several imperfect recordings of one event
  and produces one reconstructed, attributable record. (Also: *"reconstruct the
  record."*)
- The natural surface is a CLI with one subcommand per stage:
  `clearrecord ingest | align | transcribe | reconcile | export`.

## Scope / boundaries

- **Local-first, open-source, work-unrelated.** Do not import work/company
  code, assets, credentials, recordings, datasets, partner names or internal
  docs into the repo. (Provenance note; not a legal opinion.)
- **Define it from a generic public problem statement**, not from
  "recreate what was built at work." Keep Git history from day one.
- **Offline and subscription-free**: own the hardware and models; no cloud
  cost; no dependence on a third-party service continuing to exist.

## Backends

- **Support all three major desktop compute families** behind one interface,
  inspired by the observation (Marco Arment / Overcast) that Mac frameworks are
  excellent for on-device transcription: **Apple** (Metal / Core ML / ANE),
  **NVIDIA** (CUDA), **AMD Radeon** (ROCm / Vulkan). No single-vendor lock-in.

## Reliability

- Ingestion and processing must be **timestamped and chunked/durable**; the
  pipeline must be **resumable**, so workstation/OS instability and multi-GB
  recordings do not destroy progress.

## What is deliberately NOT part of the open repo

The exotic, invention-grade ideas — distributed-transmitter spatial
arrays/beamforming, camera fusion, relative 3-D speaker localization
(acoustic triangulation, DOA/TDOA fusion), cross-device clock sync as a product
feature, network-attached world-reconstruction pods — are **not** in scope for
this repository. See `docs/architecture.md` §7.

## Command naming (2026-09-13)

- The CLI **command** is spelled `clear-record`, not `clearrecord`: *"the cli
  reads as 'clear-record' instead of 'clearrecord'"*. This supersedes the
  `clearrecord` spelling in "Project framing" above; it concerns the command
  only — the distribution name (`cr-cli`) is unaffected (ADR-0009).

## Distribution naming (2026-09-13)

- Owner directive: *"let's do a clear-record shim package so uvx can go fish
  `clear-record` and run it as `clear-record`."* The public install name is
  therefore **`clear-record`** — a facade dist over the `cr-cli`
  implementation package, so `uvx clear-record` resolves and runs the command
  with no warning. `cr-cli` keeps its name as the implementation the facade
  calls into; it declares no console script of its own, so the facade is the
  sole owner of the `clear-record` command (ADR-0009).

## Build toolchain (2026-09-13)

- Owner directive: *"use `uv_build` to replace `hatchling`, so we are uv
  front-to-back, end to end."* Every workspace member and the virtual root
  declare `uv_build` in `[build-system]` (ADR-0010).
- Owner resolution of the shim shape: *"a 'import main' from cr_cli style shim —
  simple and easy … cr_cli's entry point can stay behind the scenes."* The
  `clear-record` dist is therefore a small **facade module**
  (`clear_record.main` → `cr_cli.cli.main`), not a metadata-only dist:
  `uv_build` requires every dist to ship a module. This supersedes the earlier
  metadata-only framing of the facade; the Distribution-naming directive above
  is unchanged (ADR-0009, ADR-0010).

## Versioning and release train (2026-09-13)

- Owner directive, verbatim: *"we need a work-in-progress or `-dev` build tag.
  v0.1.0 is tagged, and we would tag this as v0.1.1; let's consider we would do
  `releases/v0.1.x` release train and current main = that release train"*.
- The three specifics below are owner-selected decisions; the option wording is
  agent-authored:
  1. [DECISION] **`main` carries `0.1.1.dev0`**, bumped for each snapshot — the
     published version is a dev series, not a hand-edited release number.
  2. [DECISION] **WIP/dev builds publish to TestPyPI**, so pre-releases exercise
     the real index path without claiming PyPI versions.
  3. [DECISION] **The release train is lazy**: `main` *is* `releases/v0.1.x`
     today; the branch is cut only when 0.2 development starts.
  See [`docs/adr/0011-versioning-and-release-train.md`](../adr/0011-versioning-and-release-train.md).

## Dev builds and TestPyPI (2026-09-13)

- Owner directive, verbatim: *"dev builds are not going to test.pypi.org, dev
  builds (if any) can become a artifact of automated pipeline"*. This reverses
  decision 2 in "Versioning and release train" above: dev builds are **CI
  workflow artifacts**, never published.
- The two rehearsal specifics below are agent-authored framing of the owner
  directive; they are **not** owner quotes:
  1. [DESIGN] The TestPyPI rehearsal (`publish-testpypi.yml`, manual
     `workflow_dispatch` only) qualifies the **exact commit** that becomes the
     release tag.
  2. [DESIGN] TestPyPI is a separate index, so rehearsing `X.Y.Z` there does
     **not** consume `X.Y.Z` on PyPI.
- The automatic `v*.dev*` → TestPyPI snapshot workflow was deleted. PyPI stays
  tag-triggered (`vX.Y.Z` → `publish.yml`). See
  [`docs/adr/0011-versioning-and-release-train.md`](../adr/0011-versioning-and-release-train.md)
  (`## Update (2026-09-13)`) and [`docs/releasing.md`](../releasing.md).

## Release candidates and stable-only PyPI (2026-09-13) (superseded below)

- Owner directive, verbatim: *"let's make a change. the rc build could go
  test-pypi; real pypi sees stable releases"*. Release candidates (`X.Y.ZrcN`)
  go to **TestPyPI**; **PyPI receives stable releases only**. The TestPyPI lane
  stays manual (`workflow_dispatch` only) and still refuses `.devN`; dev builds
  remain CI workflow artifacts.
- The two mechanics below are agent-authored framing, **not** owner quotes:
  1. [DESIGN] `scripts/bump-version.py` gains `--rc` (`just bump-rc`): `X.Y.Z`
     → `X.Y.Zrc1`; `X.Y.ZrcN` → `X.Y.Zrc{N+1}`, mirroring `--dev`. Its
     validation now accepts stable, `a`/`b`/`rc`, and `.devN`.
  2. [DESIGN] `publish.yml` excludes pre-release tags (`!v[0-9]*rc*`,
     `!v[0-9]*a*`, `!v[0-9]*b*`) alongside dev tags, and its publish job refuses
     a manifest version that is not stable `X.Y.Z`. See
     [`docs/adr/0011-versioning-and-release-train.md`](../adr/0011-versioning-and-release-train.md)
     (`## Update (2026-09-13)`) and [`docs/releasing.md`](../releasing.md).

## Release candidates also publish to PyPI (2026-09-13, owner acceptance)

- Owner acceptance, verbatim: *"chat gpt recommends rc build also goes real
  pypi. I think I'd accept."* This is a **hedged acceptance** ("I think I'd
  accept"), not a firm directive. It **supersedes** the "Release candidates and
  stable-only PyPI" entry above: **PyPI receives release candidates as well as
  stable releases.** A `vX.Y.ZrcN` tag publishes the candidate to real PyPI as a
  PEP 440 pre-release; a `vX.Y.Z` tag publishes the stable release. PyPI is
  **publishable as an `rc`, never as a dev version** — dev builds (`X.Y.Z.devN`)
  remain CI workflow artifacts and are never published.
- The mechanics below are agent-authored framing, **not** owner quotes:
  1. [DESIGN] `publish.yml` drops the `rc`/`a`/`b` tag exclusions; its tag filter
     is back to `["v[0-9]*", "!v[0-9]*.dev*"]`, and its publish job refuses only
     a `.devN` manifest version. Stable and `rc` tags therefore both publish.
  2. [DESIGN] The TestPyPI lane (`publish-testpypi.yml`) rehearses the candidate
     before the `vX.Y.ZrcN` tag publishes it to PyPI; the dev-refusal guard is
     now identical in both lanes.
  3. [DESIGN] A PyPI pre-release is opt-in for installers: `pip`/`uv` need
     `--pre`, an explicit pin, or no stable version satisfying the range. See
     [`docs/adr/0011-versioning-and-release-train.md`](../adr/0011-versioning-and-release-train.md)
     (`## Update (2026-09-13)`) and [`docs/releasing.md`](../releasing.md).

## Single published distribution (2026-09-13)

- Owner proposal, verbatim: *"I propose we hide all the `cr_*` layers behind
  the scene … I'm not sure I'd release all `cr_*` as different wheels"*. This
  is an **owner proposal**, hedged ("I propose…", "I'm not sure I'd…"), not a
  firm directive. The four `cr_*` layers therefore collapse into **one published
  distribution**, `clear-record`, with the layers kept as subpackages
  (`clear_record.{core,engine,providers,cli}`). `uv_build` ships one import
  package per dist, so keeping four top-level modules would mean four wheels
  (ADR-0012).
- The mechanics below are agent-authored `[DESIGN]` framing, **not** owner
  quotes:
  1. [DESIGN] The vendor-free boundary moves from `cr-core`'s empty dependency
     list (a packaging fact) to
     `packages/clear-record/tests/test_layering.py`, an explicit stdlib-`ast`
     import-boundary test.
  2. [DESIGN] The workspace is kept (one member) so a future GUI/MCP member is
     a new `packages/*` member, not a reparenting; the root's aggregate extras
     now reference `clear-record[apple|nvidia|amd|all]`.
  3. [DESIGN] `scripts/bump-version.py` and the publish workflows are left
     working as-is; the release machinery is simplified for one publisher in a
     follow-up slice. See [`docs/adr/0012-single-distribution.md`](../adr/0012-single-distribution.md).
