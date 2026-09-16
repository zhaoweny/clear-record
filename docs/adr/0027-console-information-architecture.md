# ADR-0027 — Console information architecture: pages, a settings home, and guided setup

Status: active
Date: 2026-09-15

## Context

- [FACT] The console has been **one page plus fragments**: `index.html` renders a
  sidebar and a `#detail` panel, and every `/ui/*` response swaps into
  `#detail`. Project selection, a meeting review, the glossary, webhooks and
  agent setup all live on that one page (ADR-0016, ADR-0023).
- [VOICE: owner, 2026-09-15] The owner steered a page-level structure, verbatim:
  *"I imagine we'd have a few pages: project page ... settings page ... setup
  wizard ... setup wizard for agents"*, including a first-run walkthrough with
  *"a synthetic tape of 'hello world' in user's language"*.
- [FACT] ADR-0018 already made the guided setup **wizard-shaped** and preferred
  guide-first over bundling a harness.
- [FACT] The console is htmx 4 + Alpine over server-rendered Jinja, dark/light,
  bilingual (EN / zh-CN), localhost-only with no auth (ADR-0021), and now has a
  Playwright gate that drives the real server and captures screenshots.
- [FACT] The setup state already exists (`agent-setup.json`: endpoint, model,
  harness, mcp_config) with a tri-state view (ready / not_configured / problem).

## Decision

- [DECISION] **Pages, with real URLs.** The console stays one htmx/Alpine app but
  gains top-level routes, and the **URL is the source of truth** for which page
  and which project is shown. Navigation uses `hx-boost` so a click is fast,
  while a plain link still works without JavaScript. No SPA, no client router.
- [DECISION] The sitemap: `/` and `/projects/<slug>` (Projects);
  `/projects/<slug>/meetings/<meeting>` (meeting review); `/settings` and
  `/settings/<section>` (Settings); `/setup` (first run / after update);
  `/setup/agent` (the agent wizard). `/ui/*` remains the in-page fragment
  surface; `/api/*` is untouched.
- [DECISION] **The sidebar becomes project navigation**, not a junk drawer:
  webhooks and agent setup move to Settings, and the project page gains
  sub-tabs — Overview, Meetings, Glossary, Media (every meeting's tapes and
  transcripts in one inventory).
- [DECISION] **Settings v1 is read-mostly.** It shows status and keeps the writes
  the console already performs (the agent endpoint and the MCP client config);
  models, webhooks and storage are displayed with the config path to edit, so the
  first pass adds no new config-write surface.
- [DECISION] **Setup is non-blocking.** A fresh install lands on `/setup`; after
  an update a dismissible notice points at it; a returning user is never
  redirected away from their work. "After an update" is detected by a **setup
  version marker** recorded in the setup state.
- [DECISION] The **agent wizard is a step-flow** (Endpoint → Harness → MCP
  config → Try it) inside setup. Its last step generates a **hello-world tape in
  the user's language with the system TTS**, runs it through ingest → transcribe,
  shows the transcript, then launches one agent task to prove the MCP round-trip.
  A TTS adapter is a **provider** (the vendor-free core rule, ADR-0012), and the
  generated tape records its provenance.

## Rationale

- Pages give the console what a single panel cannot: deep links, a working back
  button, a refresh that keeps your place, and somewhere to put settings that is
  not a sidebar.
- `hx-boost` keeps the speed the htmx model already provided without asking the
  user to learn a client-side router.
- Read-mostly settings match the existing trust boundary: the config file is the
  user's, and the console already knows how to write exactly two things into it.
- A hello-world tape is the shortest honest path from "installed" to "I saw it
  work"; generating it in the user's language is the difference between a demo
  and a product.

## Discarded alternatives

- **htmx panel swaps with no URL** (today's shape) — no deep links, no
  back/forward, and a refresh loses the selection; this is the thing the owner is
  explicitly moving away from.
- **A blocking wizard** — hostile to a returning user, and unnecessary: a fresh
  install has nothing else to show.
- **A fully editable settings console now** — multiplies the config-merge and
  write-guard surface before the pages exist.
- **Committed per-locale clips instead of system TTS** — no voice dependency, but
  it fixes the set of supported languages and ships audio we would have to
  licence and prove we generated; kept as the fallback when no system voice
  exists.
- **A tone-only smoke test** — deterministic, but it does not prove
  transcription, which is the one thing the walkthrough is for.

## Consequences / review hook

- The `#detail` contract in `app.css` and the tests is **superseded** for
  navigation while remaining the in-page swap target where it still applies; the
  e2e gate grows a spec and screenshots per page.
- New surfaces mean **new strings**; every one needs extraction and a zh-CN
  translation (the i18n guard enforces it).
- The TTS seam must not leak into `clear_record.core`; a missing system voice is
  a state, not an exception, and the wizard must say so plainly.
- Revisit a lighter "what changed" variant if the full wizard proves noisy across
  frequent dev releases.

## Update — 2026-09-15: four surfaces, four jobs (owner steering refinement)

Owner-relayed refinement. The four surfaces are not navigation categories; they
are four different jobs, and holding that line is what keeps the console from
becoming a dashboard.

- [DESIGN] **Projects is the daily workspace** (~90% of use): project list →
  project detail → tapes, transcriptions and the project glossary. It must not
  drift into an administration screen.
- [DESIGN] **Settings is the control plane**: endpoints, MCP, webhooks, storage,
  runtime knobs, status and diagnostics — configured occasionally, read often.
- [DESIGN] **Setup is system readiness**: first launch and upgrades, the path from
  installed to usable.
- [DESIGN] **Agent setup is integration readiness, and it is one reusable flow,
  not a subsystem.** The same flow is launched at first run and later from
  Settings → Agent: one implementation, two entry points.
- [DESIGN] **The hello-world tape is the onboarding acceptance test**, not
  tutorial content: create the tape → transcribe it → show the transcript →
  expose it over MCP → a connected agent answers something about the tape. The
  wizard's last screen is then "the whole system has worked once", and the same
  flow is a **permanent diagnostic**: re-running it localizes a fault to
  transcription, clear-record itself, MCP transport, harness configuration, or
  the model.
- [DESIGN] **No dashboard.** Launching the console lands on a useful Projects
  page; there is no summary-of-everything home. Structural navigation is enough
  for the project counts this tool sees, and full-text search is deferred
  (SQLite FTS is the future answer, not a navigation workaround).
- [DESIGN] Optimize the console as a **desktop productivity application**, not a
  marketing/SaaS dashboard: information hierarchy, workflow continuity, sensible
  density, keyboard-friendly interaction and clear system state over decoration.

## Update — 2026-09-16: the check's boundaries (ticket 05, implemented)

Implementation narrowed the "launches one agent task to prove the MCP
round-trip" clause of the 2026-09-15 decision to what the offline gates can
honestly assert:

- [DECISION] The **Try it** check runs the local chain synchronously — create the
  hello-world tape → ingest → transcribe → read the transcript with the same read
  the MCP tool exposes — and reports the exact MCP client entry
  (`clear-record mcp`) and the exposed tool (`read_transcript`). It does **not**
  start an agent task: that needs a real model endpoint, and `just verify` /
  `just e2e` stay offline and deterministic.
- [DECISION] **Every anticipated failure is a finding that names the leg**:
  `tts` (no system voice), `backend` (no ASR backend), `model` (no checkpoint on
  disk, and none is ever downloaded), `transcribe` (the decode failed). The
  finding carries the CLI's/service's own message; the console only translates it.
- [DECISION] The **agent-answers** leg is proven on demand by the optional,
  bring-your-own-key (BYOK) `just agent-drive` workflow (ticket 07), not by the
  console check.
- [DECISION] The **same** check is the permanent diagnostic at Settings → Status;
  `/setup/agent` and `/settings/agent` render the one four-stage flow
  (Endpoint → Harness → MCP config → Try it).

## Update — 2026-09-16: one job per setup step, and the hello-check truth split

A follow-up refinement to the ticket-04/05 console work (owner-approved).

- [FACT] The setup wizard's second step was named **Models** and its copy
  presented a checkpoint as a hard prerequisite, conflating two different
  models: the **ASR checkpoint** this step is about and the small *LLM* the
  Agent step can pull into Ollama (`DEFAULT_SMALL_MODEL` = `qwen2.5:1.5b`). It
  also pointed at read-only Models settings as somewhere to "fetch" a
  checkpoint, and it was conditionally false on macOS 26, where the preferred
  backend (`apple-speech`) is model-free.
- [DECISION] The wizard is **one job per step**: Welcome → Transcription →
  Agent → Try it, every step still skippable. The second step is renamed
  **Transcription** (id `setup-transcription`) and owns ASR backend and
  checkpoint readiness. It renders three states from
  `service.agent_flow.transcription_status()` — **ready** (`state == "ok"`),
  **needs backend** (`state == "backend"`), **needs model** (`state == "model"`)
  — and shows the resolved backend, the models directory and what is on disk.
- [DECISION] Its copy states what is true per state: a model-free backend needs
  no checkpoint on this system; a missing checkpoint is downloaded by the first
  transcription run (ggml models come from Hugging Face on first use) or can be
  placed in the directory. It never points at the Agent step or claims Models
  settings can fetch. The `/setup` route passes
  `transcription=transcription_status()`; it no longer passes
  `models_dir`/`models_present`.
- [DECISION] The last step is renamed **Try it** (id `setup-try`) and still
  points at the one acceptance check: Settings → Status, or the Agent step's
  Try it stage.
- [DESIGN] The hello-check success screen is split into what the check actually
  proves: **Transcription ready** (the local chain reached a transcript, with
  its segment count), **Agent integration configured** (the MCP client config
  and harness are present; otherwise the screen names what is missing), and
  **Agent round-trip verified**, which the console cannot prove and therefore
  never shows as a green badge — it points at the optional `just agent-drive`
  workflow. The old "the whole system worked once" badge was re-scoped to what
  the check actually proves.

