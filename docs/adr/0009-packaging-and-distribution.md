# ADR-0009 — Packaging and distribution: five dists, exact pins, OpenID Connect (OIDC) publishing

Status: active
Date: 2026-09-13

## Context

- [FACT] ADR-0004 fixes the uv workspace: five members under `packages/`, each
  a `src/` layout with a dist name (`cr-core`, `cr-engine`, `cr-providers`,
  `cr-cli`, `clear-record` — the last is a facade, see the Decision), and left a
  review hook: "Revisit on the first real packaging/distribution push."
- [FACT] ADR-0007 left the same review hook for the deployment directories.
- [FACT] ADR-0004 also records that the root project is a **virtual project**
  (`package = false`), never built or published; only the five members are real
  packages.
- [FACT] Every member builds with `uv_build`, and the virtual root declares the
  same backend (ADR-0010); the backend choice does not change the five-dist set
  or the facade shape decided here.
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
- [VOICE: owner] 2026-09-13: "let's do a clear-record shim package so uvx can
  go fish `clear-record` and run it as `clear-record`." The owner directs that a
  **`clear-record` facade dist** exist so `uvx clear-record` resolves and runs
  the command cleanly; see the Decision below. (Recorded verbatim in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).)
- [REQ] The hard rule from `AGENTS.md`/ADR-0004 stands: `cr-core` must stay
  **vendor-free** — no CUDA, ROCm, Metal/CoreML, torch/tensorflow or specific
  ASR library.

## Decision

- [DECISION] Publish all five workspace members to PyPI. The three **library
  dists keep their current names** — `cr-core`, `cr-engine`, `cr-providers` —
  released in lockstep. The **CLI implementation package** is `cr-cli`; the
  **public install name** is `clear-record`, a **facade** that ships a small
  `clear_record` module (re-exporting `cr_cli.cli.main`) and depends on
  `cr-cli==X.Y.Z`. Once published the user install is
  `uv tool install clear-record` (equivalently `pipx install clear-record`), and
  `uvx clear-record` runs it without installing.
- [DECISION] The **console-script command** is **`clear-record`**, and exactly
  one dist owns it: the **`clear-record` facade** declares
  `clear-record = "clear_record:main"`. The `cr-cli` implementation package
  **declares no console script** (owner direction, 2026-09-13 — see Context).
  The facade re-exports `cr_cli.cli.main`, so the command resolves to
  the implementation; there is no duplicate owner.
- [DECISION] The `clear-record` dist ships a **facade module**
  (`clear_record/__init__.py`, `from cr_cli.cli import main`); every dist must
  carry an import package, and `uv_build` refuses to build a dist without one.
  The facade exists so resolvers and `uvx` see a distribution whose *own*
  metadata provides the `clear-record` command, which avoids the
  dependency-provided-command warning (in the form of: *An executable named
  `clear-record` is not provided by package `clear-record` but is available via
  the dependency `cr-cli`*).
- [DECISION] Intra-project dependencies are **exact-pinned** (`==`) per release:
  the built metadata for each member names its sibling at the exact released
  version (e.g. `cr-engine==0.1.0` → `Requires-Dist: cr-core==0.1.0`). The five
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
- [DECISION] Owner directive (2026-09-13 — see Context) settles the former
  `[OPEN]` CLI-distribution-name question: the public install name is
  **`clear-record`**, a **facade** over the `cr-cli` implementation package.
  `cr-cli` keeps its name and holds the implementation (`cr_cli`); it declares
  no console script, so `uvx cr-cli` provides no command by design. The facade
  owns the `clear-record` entry point and depends on `cr-cli==X.Y.Z`, so
  `uvx clear-record` resolves and runs with no warning. Publish `cr-cli` before
  (or with) `clear-record`, since the facade depends on it.

## Rationale

- **Five dists, one release train.** The members are separately importable
  (ADR-0004) — the `clear-record` facade too — so they stay separate
  distributions; exact pins turn "one release" into something the resolver
  enforces rather than something maintainers remember.
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
- **Publish only `cr-cli` and rely on it for the command** — `cr-cli` declares
  no console script, so `uvx cr-cli` cannot run anything and the public
  `clear-record` install name would not resolve at all; a `clear-record` facade
  that declares the entry point itself is what makes `uvx clear-record` run
  (owner direction, 2026-09-13).

## Consequences / review hook

- **Version lockstep is now a maintenance obligation.** Every release must bump
  all five `version =` fields, the sibling `==` pins, and `uv.lock` together;
  [`docs/releasing.md`](../releasing.md) is the checklist.
- **A PyPI version can never be reused or overwritten.** A bad release is fixed
  by publishing a new patch version (optionally yanking the bad one), never by
  re-uploading.
- Resolves the review hooks in ADR-0004 and ADR-0007. The concrete dev/user
  deployment layout remains ADR-0007's subject.

## Update (2026-09-13) — the published set is one dist

Owner (2026-09-13, verbatim): *"I propose we hide all the `cr_*` layers behind
the scene … I'm not sure I'd release all `cr_*` as different wheels"* — recorded
in [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md). The five-dist
Decision, facade shape, exact `==` pins and multi-publisher setup above are kept
for the record; this Update supersedes them for the published set.

- [DECISION] The published set is now **one** dist, `clear-record` (import
  `clear_record`), with the former `cr-core` / `cr-engine` / `cr-providers` /
  `cr-cli` layers as subpackages. See
  [ADR-0012](0012-single-distribution.md).
- [DECISION] The published wheel declares only **`numpy`/`soundfile`**; there are
  no `cr-*` dependencies and no `[tool.uv.sources]`. The vendor-free boundary is
  no longer the packaging fact it was ("`cr-core` has no deps"); it is enforced
  by `packages/clear-record/tests/test_layering.py`.
- [DESIGN] The lockstep bump, five pending publishers and multi-dist build are
  now overbuilt for one dist. `scripts/bump-version.py` and the publish
  workflows are left **working as-is** in this slice; reshaping them for a single
  publisher is a follow-up slice.
