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

## Project Console / GUI (2026-09-14)

- Owner brief, verbatim (line breaks are the owner's):

  > I want a GUI with simple-ish feature set. It would be a sub command for clear
  > record, and it would be pyside2 or web based. Main features:
  > - select a local set of audio tape
  > - run through the clear record pipeline
  > - show a progress bar and estimate
  > - maintain a multi-project glossary table
  > - archive the incoming tape set and its transcript result
  > - user brings their models and agents
  > - user's ai agent does the glossary collection, check the transcript, and
  >   produce a meeting minutes for given meetings, which belongs to a project.
- The selections below are **owner decisions**; the option wording was
  agent-authored. They are not yet implementation — the scoped design lives in
  the local tracker (`.scratch/project-console/`, gitignored) and lands as a spec
  plus tickets.
  1. [DECISION] The UI is a **local web UI**, not a Qt desktop app. PySide2 is
     unusable on this project's Python (its wheels stop at 3.10; the project
     requires ≥3.12), so "pyside2 or web" resolves to web.
  2. [DECISION] The agent seam is **both**: an MCP server the owner's agent can
     connect to, and a user-configured command template the console can invoke.
  3. [DECISION] **App-owned registry + user-chosen archive root**: the
     project/glossary/meeting registry lives under the XDG data dir, while
     recordings and archives stay in user-chosen directories (consistent with
     ADR-0006/ADR-0007).
  4. [DECISION] Build order is **headless service first, thin GUI second**.
  5. [DECISION] The registry is **SQLite + file artifacts** (no audio blobs in
     the database).
  6. [DECISION] The web surface is **FastAPI + a no-build frontend**.
  7. [DECISION] The work is **two new workspace members** — a headless service
     and a GUI — matching ADR-0012's anticipated "GUI/MCP member". The published
     `clear-record` dist keeps `numpy`/`soundfile` only.

### Packaging revision, and the harness prior art (2026-09-14)

- Owner directive, verbatim: *"I expect we can bundle the web up with our wheel
  so end user do 'clear-record web' and it just works."* Then, opening the shape:
  *"we can build a '[web]' extra and hide web and UI behind it. You may decide
  which is better - ship it together or dedicate command and wheel."*
- Owner direction on the agent harness, verbatim: *"For potential agent harness
  maa-whirlwind can become a priori art."*
- **Agent resolution of item 7 above** (the owner delegated the packaging
  choice): the web and service **code** ships in the **one `clear-record` dist**
  (as `clear_record.service` + `clear_record.web` subpackages, since `uv_build`
  ships one import package per dist), while the web stack is gated behind a
  **`[web]` extra** (`fastapi`/`uvicorn`) and the MCP SDK behind `[agents]`. So
  the base dist *still* depends only on `numpy`/`soundfile` — item 7's "two new
  workspace members" is superseded, item 7's dependency line is preserved. The
  command is `clear-record web`. See
  [ADR-0013](../adr/0013-bundled-web-and-service-surface.md).
- **Harness prior art** is the owner's own [maa-whirlwind
  ADR-0005](https://github.com/zhaoweny/maa-whirlwind): an MCP server exposing
  semantic tools over shared services, a reference external MCP consumer, **BYOK**
  (no bundled provider key), and the harness never entering the core. The
  clear-record agent seam follows that shape (MCP server + a command-template
  runner), also recorded in ADR-0013.

### Desktop app build (2026-09-14)

- Owner directive, verbatim: *"In the end build a pyinstaller spec, which means
  we could offer Mac and windows build with minimal friction to average
  person"*.
- [DECISION] A **PyInstaller spec** plus a `just app` recipe freeze the console
  into a double-clickable desktop app (`clear-record-web` / `clear-record.app`)
  and the full CLI (`clear-record`), built per-OS and kept as **CI workflow
  artifacts** — never published to an index. Builds are unsigned; the Gatekeeper
  / SmartScreen first-run workarounds are documented, and signing/notarization
  is an explicit future step. No model weights or keys are bundled. See
  [ADR-0014](../adr/0014-desktop-app-distribution.md).

## App shell: htmx/Alpine, PySide6 tray, pi-agent (2026-09-14)

- Owner directives, verbatim:
  - *"so we would have: a python back-end, a python mcp service, a pi-agent
    powered local agent doing the useful stuff."*
  - *"I'd like to change my decision: let's make a pi-agent compatible vue /
    react app and produce a bundle that our fastapi could serve it; we need
    fastapi or something for the mcp anyway."*
  - *"and beside the obvious web ui route, let's make a pyside2 native tray icon
    app, for supervising the service and give the whole app an entry point"*
  - correction: *"nah, I'd like to change it again. let's try htmx and alpine.js
    for the web-ui."*
- [DECISION] The application shape is **Python backend + Python MCP service +
  pi-agent-powered local agent**. The local agent talks to the MCP server; it is
  a replaceable client, never imported by `clear_record.core` (BYOK — no key or
  agent runtime is bundled). Prior art: maa-whirlwind ADR-0005.
- [DECISION] The web UI is **htmx + Alpine.js**, server-rendered by FastAPI, with
  the two libraries vendored under `clear_record/web/static/` — **no build
  step**. This **supersedes** the briefly-stated Vue/React direction (and
  ADR-0013's embedded Python-string assets). See
  [ADR-0016](../adr/0016-app-shell-htmx-tray-pi-agent.md).
- [DECISION] A **native tray supervisor** gives the app an entry point:
  `clear-record tray` runs the console in the background and offers open /
  status / quit from the system tray. The owner asked for "pyside2"; **PySide2
  cannot work** on this project's Python (its wheels stop at 3.10, the project
  requires ≥3.12), so this is **PySide6** — noted to the owner at the time.
  It is an optional `tray` extra so the base install stays audio-only.

## Release train cut: `releases/v0.1.x` + a 0.2.x trunk (2026-09-14)

- Owner directive, verbatim: *"cut release track releases/v0.1.x and main is
  current dev / trunk for v0.2.x"*.
- Owner choice of cut point (agent-offered options): the maintenance branch is
  cut from the **`v0.1.1` tag**, **not** from `main`'s then-HEAD, so a 0.1.x
  patch release carries none of the unreleased console / run / archive / MCP
  work. The literal ADR-0011 "cut at current `main`" reading was rejected by the
  owner in favour of the cleaner released-line cut.
- [DECISION] `main` becomes the **0.2.x development trunk** at `0.2.0.dev0`;
  `releases/v0.1.x` is the 0.1 maintenance line. See
  [ADR-0011](../adr/0011-versioning-and-release-train.md)'s 2026-09-14 Update and
  [`docs/releasing.md`](../releasing.md). Nothing is pushed; the branch is local
  until the owner chooses to publish it.

## System-native transcription backends (feature request, 2026-09-14)

- Owner request, verbatim: *"note 2 new feature requests. we'd like to system
  default transcription services, like apple SpeechTranscriber and Windows
  Microsoft.Windows.AI.Speech"*.
- [REQ] Recorded as a **feature request**, not a decision. The scoped design,
  the platform `[FACT]`s and the `[OPEN]` items live in the local tracker
  (`.scratch/system-speech-backends/`); tickets 01–03 cover the backend seam, the
  Apple `SpeechTranscriber` backend and the Windows `Microsoft.Windows.AI.Speech`
  backend.
- [FACT] This **changes an ADR-0005 property**: today every backend drives the
  system `whisper-cli` + a ggml plugin. A system-native backend does not, so the
  work needs a fresh ADR (tracked as ticket 01) rather than a silent extension.

## Transcription profiles and auto mode (feature request, 2026-09-14)

- Owner request, verbatim: *"feature request: provide sufficent knobs to build a
  'recommended default / auto mode' and profiles like 'fast, balanced, accurate,
  custom'"*.
- [REQ] Recorded as a feature request, not a decision. Scoped in
  `.scratch/transcription-profiles/` (spec + tickets 01–04): one shared
  run-options + profile table, the missing decoder knobs, an explainable `--auto`
  resolver, and the profile surface on CLI/MCP/web.
- [FACT] It **unblocks two recorded `[OPEN]`s**: ADR-0017's "MCP `start_run`
  cannot set backend/model/language" gap, and the web console's inability to set
  run options — both want the run-options type owned outside `cli`.

## Backend hardware research: Intel, DGX Spark, mobile/edge (2026-09-14)

- Owner request, verbatim: *"feature request: research intel transcript story so
  we have all major pc vendor as the backend; then we research dgx-spark (which
  is a nvidia powerhouse) story, as well as mobile chip story like apple,
  qualcomm and mediatek, rockchip"*.
- [REQ] Recorded as a **research** request — not a decision and not an
  implementation. Tracker `.scratch/hardware-backends/` (spec + tickets 01–03);
  findings land as dated notes in `docs/research/`, matching the owner's
  convention (maa-whirlwind keeps `docs/research/`).
- [FACT] The research feeds two existing docs rather than creating a decision:
  **ADR-0005** (whether the "one interface, three families" story becomes four,
  and whether a non-`whisper-cli` runtime such as OpenVINO is admitted) and
  **architecture §5/§8** (the hardware-lab table and the "NVIDIA not hot-tested"
  gap).

## `whisper-cli` is a fallback, not the substrate (2026-09-14)

- Owner position, verbatim: *"we are expanding to cover apple native and windows
  native path anyway, so we are not strictly bound to just whisper-cli - it's a
  good and honest fallback at this moment"*.
- [DECISION] Native, OS-provided transcription paths (Apple `SpeechTranscriber`,
  Windows `Microsoft.Windows.AI.Speech`) are **first-class backends**;
  `whisper-cli` + a ggml plugin is the **portable fallback**. This **supersedes**
  the "every backend drives `whisper-cli`" property — recorded as an Update on
  [ADR-0005](../adr/0005-transcription-backend-strategy.md).
- [OPEN] Which backend is the **default** on a platform (native vs. the fallback)
  is left to the profiles / auto-mode work. The owner framed the balance as
  current, not permanent (*"at this moment"*), so it is expected to move as the
  native paths land.

## Open-question grilling, rounds 1–4 (2026-09-14)

Owner answers to the consolidated open questions, presented three per round. The
scoped consequences live in the trackers and ADRs; this is the intent record.

- [DECISION] **`--auto` is opt-in**; with no flags, behaviour is unchanged.
- [DECISION] A profile **tunes knobs only**; **backend selection gets its own
  `auto` knob** — owner, verbatim: *"maybe back-end itself deserve a 'auto' knob,
  but yes, profile tunes knobs"*.
- [DECISION] **Built-in profiles only** for now
  (`fast`/`balanced`/`accurate`/`custom`); config-file profiles deferred.
- [DECISION] **The default agent path is the web UI** — owner, verbatim: *"the
  default path should be clicking around on the web UI, in my opinion. then if
  they want custom command, they can talk to the agent"*. The command template is
  the advanced path; the `[agent]` config keys are plumbing, not a product surface.
- [DECISION] Transcript-check produces a **corrected revision + change list**.
- [DECISION] Archive **copies** tapes, never hardlinks.
- [DECISION] **Native first, `whisper-cli` fallback** as the platform default.
- [DECISION] **Apple-native first; Windows-native deferred** (the MSIX +
  `systemAIModels` requirement collides with the PyInstaller app).
- [DECISION] The service CLI stays **`serve` + `mcp`**; a read-only convenience
  CLI is deferred.
- [DECISION] **Push** `main` + `releases/v0.1.x` upstream.
- [DECISION] **Ship the macOS app unsigned**, keeping the documented Gatekeeper
  workaround (no Apple Developer Program spend).
- [DECISION] Flatpak keeps **`--share=network`** for first-use provisioning.

## Agent task execution: no bundled harness (2026-09-14)

- Owner answer to the last open question — *"should we build / bundle a genuine
  agent harness for jump start the agent experience?"* — is **no bundle**: task
  pipelines over a **BYOK endpoint**. See
  [ADR-0018](../adr/0018-agent-task-execution.md).
- [DECISION] Three rungs, in setup order: **endpoint (default)** — a
  BYOK/local OpenAI-compatible server, which is what the web UI runs; **command
  (advanced)**; **MCP (power)** for a real agent.
- [FACT] The rationale turns on task **shape**, not preference: glossary
  collection, transcript check and minutes are structured generation, not agentic
  loops, so a harness would import a Node runtime (in the wheel *and* the desktop
  bundle) for capability the default path does not use. maa-whirlwind bundles
  pi-agent because *its* tasks really are agentic.
- [REQ] BYOK is enforced: the credential is read from the environment, never
  stored in config/registry, never bundled; a local endpoint needs no key.
- [VOICE: owner, 2026-09-14] Refinement on onboarding, verbatim: *"I mean, the
  user might want to iterate on it, so at that point we might bundle 1 simple
  harness or have a guided setup so we ease the onboard process"*.
- [DECISION] Split the concern: **iteration** is already covered (prompt/context
  hash + draft accept/reject lets a user re-run and compare); **onboarding** is
  the open half. Preferred order is **guide first** — a guided setup (ticket 20)
  adds no runtime and is reversible; bundling **one** simple harness remains the
  **escalation path**, with a trigger rather than a date. Recorded in
  [ADR-0018](../adr/0018-agent-task-execution.md)'s 2026-09-14 Update.
- [VOICE: owner, 2026-09-14] Made the iteration half concrete, verbatim: *"the
  user might want to tell a story or iterate the glossary, or do back and forth of
  glossary <-> actual transcript, till it's tuned to their need. at that time we
  might become a simple mcp service and let the agent to do the heavy lifting"*.
- [DECISION] That loop is the **driving use case for the MCP rung**: clear-record
  stays a **simple MCP service** and the **agent drives the loop** — no bespoke
  tuning UI. The shipped surface is not yet sufficient (no transcript read, no
  project/meeting notes write, `start_run` cannot set options); ticket 21 closes
  those three. The glossary-keyed chunk cache already makes re-runs re-decode.
