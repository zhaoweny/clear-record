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
