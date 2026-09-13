# ADR-0009 — Packaging and distribution: four dists, exact pins, OpenID Connect (OIDC) publishing

Status: active
Date: 2026-09-13

## Context

- [FACT] ADR-0004 fixes the uv workspace: four members under `packages/`, each
  a `src/` layout with a dist name (`cr-core`, `cr-engine`, `cr-providers`,
  `cr-cli`), and left a review hook: "Revisit on the first real
  packaging/distribution push."
- [FACT] ADR-0007 left the same review hook for the deployment directories.
- [FACT] ADR-0004 also records that the root project is a **virtual project**
  (`package = false`), never built or published; only the four members are real
  packages.
- [FACT] Before this decision the built wheels and sdists declared their
  intra-project dependencies **unversioned** (`Requires-Dist: cr-core`, no
  specifier). That is wrong for a coordinated multi-package release: `pip`/`uv`
  would resolve members from different releases.
- [FACT] v0.1 was consumed from a **source checkout only** (`uv sync
  --all-packages` + `uv run`). The packaged install path is defined here but has
  **not been exercised** — no upload has happened.
- [FACT] On 2026-09-13 the names `cr-core`, `cr-engine`, `cr-providers`,
  `cr-cli`, `clear-record` and `clearrecord` (the command's then-spelling) all
  returned **404 on PyPI** (unclaimed). Names can be taken by another party at
  any time until registered.
- [VOICE: owner] 2026-09-13: "the cli reads as 'clear-record' instead of
  'clearrecord'". The owner directs that the CLI **command** be spelled
  `clear-record`; this concerns the command only, not the distribution name.
- [REQ] The hard rule from `AGENTS.md`/ADR-0004 stands: `cr-core` must stay
  **vendor-free** — no CUDA, ROCm, Metal/CoreML, torch/tensorflow or specific
  ASR library.

## Decision

- [DECISION] Publish all four workspace members to PyPI. The three **library
  dists keep their current names** — `cr-core`, `cr-engine`, `cr-providers` —
  released in lockstep. The **CLI dist** publishes as `cr-cli` unless the open
  name question below resolves otherwise; once published the user install is
  `uv tool install cr-cli` (equivalently `pipx install cr-cli`).
- [DECISION] The **console-script command** the CLI dist installs is
  **`clear-record`** (owner direction, 2026-09-13 — see Context). This changes
  the command spelling only; the distribution name is the open question below.
- [DECISION] Intra-project dependencies are **exact-pinned** (`==`) per release:
  the built metadata for each member names its sibling at the exact released
  version (e.g. `cr-engine==0.1.0` → `Requires-Dist: cr-core==0.1.0`). The four
  `version =` fields move together; `uv.lock` is regenerated on a bump. This
  prevents a partial or mixed-version install.
- [DECISION] Publishing uses **OIDC trusted publishing** (GitHub Actions →
  PyPI, `id-token: write`, the `pypi` environment gate) — **no API tokens and no
  repository secrets**. A one-time PyPI "pending publisher" per project binds
  the PyPI project to `publish.yml`; see [`docs/releasing.md`](../releasing.md).
- [DECISION] The release builds **wheels and sdists for all members**
  (`uv build --all-packages`), not the virtual root. `dist/` is gitignored and
  never committed.
- [DECISION] The vendor-free boundary is a packaging fact, not only a code rule:
  `cr-core` declares **no third-party dependencies**. Vendor stacks stay
  optional extras on `cr-providers` (ADR-0005) and are never pulled by a default
  install.

## Rationale

- **Four dists, one release train.** The members are separately importable
  (ADR-0004), so they stay separate distributions; exact pins turn "one release"
  into something the resolver enforces rather than something maintainers
  remember.
- **Trusted publishing over tokens.** A short-lived, workflow-scoped OIDC token
  removes long-lived PyPI secrets from the repo and CI, and the `pypi`
  environment gives a human approval gate in front of every upload.
- **Wheels and sdists for every member.** A single `just build` is the only
  build entry point, matching the branching-free `just` convention.

## Discarded alternatives

- **Keep intra-project deps unversioned** — lets a user resolve mismatched
  members; unacceptable for a coordinated release.
- **One combined distribution** — would collapse the
  vendor-free boundary that ADR-0004 establishes and reintroduce vendor code
  into one dependency graph.
- **PyPI API tokens in repository secrets** — long-lived credentials to rotate
  and leak; OIDC avoids them.

## Consequences / review hook

- **Version lockstep is now a maintenance obligation.** Every release must bump
  all four `version =` fields, the sibling `==` pins, and `uv.lock` together;
  [`docs/releasing.md`](../releasing.md) is the checklist.
- **A PyPI version can never be reused or overwritten.** A bad release is fixed
  by publishing a new patch version (optionally yanking the bad one), never by
  re-uploading.
- [OPEN] Which **distribution name** should `cr-cli` publish under: the current
  `cr-cli`, or a friendlier `clear-record` (matching the project and the
  `clear-record` command)? This is about the **dist name only**; the command is
  settled as `clear-record` above. It is **not decided** — settle it before the
  first upload, while all candidate names are still unclaimed. The other three
  dist names are decided above.
- Resolves the review hooks in ADR-0004 and ADR-0007. The concrete dev/user
  deployment layout remains ADR-0007's subject.
