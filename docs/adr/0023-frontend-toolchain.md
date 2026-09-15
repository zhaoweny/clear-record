# ADR-0023 — A real front-end toolchain: source in the repo, compiled assets shipped

Status: active
Date: 2026-09-15

## Context

- [VOICE: owner, 2026-09-15] On being offered "patterns in plain CSS" versus
  "Tailwind via a precompiled artifact", the owner chose to go further, verbatim:
  *"let's do a proper front-end spin; and I guess we could have someone to handle
  the rebuild of a source release, while we ship compiled result by default. this
  also opens the path to some more complicated SPA and web apps"*.
- [VOICE: owner, 2026-09-15] The named reference is **shadcn-htmx**
  (<https://shadcn-htmx.productdevbook.com/>) — shadcn-style, MIT, htmx v4 **+
  Tailwind v4**, with a **Jinja2** flavour (our templates are Jinja2).
- [FACT] ADR-0016 chose **no build step** and vendored htmx/Alpine by hand so the
  console works offline. This ADR **supersedes that property**; its *reason* —
  offline, no runtime toolchain — is preserved by shipping compiled output.
- [FACT] `uv_build` ships non-Python files inside the import package (verified
  earlier: a probe file under `web/static/` appeared in the wheel), so compiled
  assets can ride in `clear_record/web/static/`.
- [FACT] The environment already has `bun` (the owner's maa-whirlwind repo uses it
  deliberately: workspace-local installs, a repeatable install, and npm's cache
  being read-only in sandboxes).
- [FACT] Tests currently assert the **unhashed** asset paths
  (`/static/htmx.min.js`, `/static/app.css`), which a bundler's hashed filenames
  would break — a real, small migration cost.
- [REQ] A plain `pip install` / `uvx` must still deliver a working UI, offline,
  with **no Node on the user's machine**.

## Decision

- [DECISION] **A front-end source tree lives in the repo** with its own toolchain
  and a committed lockfile. Interaction stays **htmx + Alpine over server-rendered
  Jinja** — the toolchain buys real CSS/components, not a rewrite into a SPA.
- [DECISION] **Compiled assets are shipped**: the build writes into
  `clear_record/web/static/`, which the wheel already carries. End users never
  run Node.
- [DECISION] **The compiled output is committed**, so a source release and a PyPI
  install are the same artifact, and **CI carries a freshness guard** that
  rebuilds from source and fails if the committed output differs — the same
  discipline as `uv.lock` under `just verify`.
- [DECISION] `just` owns the entry points (`web-assets` to build,
  `web-assets-check` for the guard), per the repo's tooling rule.
- [DECISION] **Choose a real bundler, not a CSS-only pipeline**, because the owner
  wants the SPA option to remain open. The exact tool is an implementation
  decision below.
- [DECISION] Tailwind (v4) is the CSS layer, and **shadcn-htmx is the component
  reference** — its Jinja2 flavour is what we copy from.
- [DECISION] The runtime banner: **no CDN, no browser-side compilation**. The
  compiled CSS/JS is vendored; the offline guarantee is unchanged.

## Rationale

- It resolves the tension the owner named directly: *fidelity to a real design
  system* on the developer side, *zero toolchain* on the user side.
- Committing the compiled output removes the "does the release have a Node build?"
  question entirely — the artifact is the same whether it came from a laptop or
  PyPI.
- The freshness guard is what keeps committed build output honest; without it,
  "committed assets" silently drift from their source, which is the failure this
  pattern usually earns.
- A bundler (rather than a Tailwind CLI alone) is what keeps the door open to the
  SPA the owner mentioned, without committing to one now.

## Discarded alternatives

- **Plain CSS, hand-written (ADR-0016's shape)** — cheapest and offline, and it
  already produced a decent pass, but it cannot use shadcn-htmx as shipped and the
  owner explicitly asked for the real spin.
- **Tailwind's browser/CDN build** — breaks the offline guarantee and ships a
  compiler to the user. Rejected outright.
- **A CSS-only pipeline (Tailwind CLI, no bundler)** — would foreclose the SPA
  path the owner explicitly wants to keep open.
- **Building assets only in CI, never committing them** — then `just build` on a
  developer's machine produces a wheel with whatever is (or isn't) in
  `web/static/`, and a source release needs the toolchain. Rejected: the owner
  asked for compiled-by-default.
- **A SPA rewrite now** — explicitly out of scope; the ask is to keep the path
  open, not to take it.

## Consequences / review hook

- **Contributors need bun (or Node) to change the UI.** That is the accepted cost;
  `just verify` must keep working for those who do not touch the front-end, and
  the freshness guard is the thing that fails loudly if they do.
- **Migrating the current assets is part of it**: htmx/Alpine become front-end
  *dependencies* (build outputs rather than hand-committed files), and the tests
  that assert `/static/htmx.min.js` and `/static/app.css` move to whatever the
  build emits (a manifest lookup, or stable filenames — decide and say which).
- [DECISION] The bundler is **Vite** (implemented 2026-09-15). It is the
  SPA-shaped default — dev server, manifest, code splitting — which is the
  owner's stated reason for a real toolchain; esbuild was smaller but would
  foreclose the SPA ergonomics. Tailwind v4 is wired through `@tailwindcss/vite`
  and its CSS-first `@theme` config.
- [DECISION] Asset filenames are **stable, unhashed** (`app.css`, `app.js`)
  (implemented 2026-09-15). The console is a localhost-only tool served from the
  package, so content-hash cache-busting buys nothing, and hashing would need a
  manifest plus a Jinja helper registered in `web/app.py`. Revisit if the
  console ever grows long-lived shared caches.
- [DECISION] The freshness guard runs in a **separate CI job**
  (`verify.yml` → `web-assets`), not inside `just verify` (implemented
  2026-09-15). The guard needs bun; the Python gate must stay runnable with no
  Node, so a contributor who never touches the UI is not blocked.
- Revisit if the compiled output proves noisy in diffs, or if the front-end grows
  enough that a committed artifact stops being reviewable.

## Update — 2026-09-15: htmx 4, the component spin, and a browser gate

- [FACT] htmx 4.0.0 released 2026-08-28. npm keeps 2.x as `latest` deliberately,
  so the pin is exact. [VOICE: owner] *"htmx 4.0 is released. and it have skills
  for you https://four.htmx.org/..."*.
- [DECISION] The console moves to **htmx 4.0.0** and **Alpine 3.17.3** (was 2.0.4
  and 3.14.9). The migration used htmx's own upgrade guide and shipped
  `upgrade-check`. Three template changes were real: colon event names
  (`hx-on::after:request`, `x-on:htmx:before:request`), a
  `htmx.config.noSwap` for 4xx/5xx (the app raises `HTTPException` and relies on
  htmx 2's no-swap), and the removal of `htmx:xhr:progress`.
- [DECISION] **Attribute inheritance is not relied on.** htmx 4 makes it opt-in;
  `upgrade-check` flags `_detail.html:36`, where the descendant `<select
  hx-get>` declares its own `hx-target`/`hx-swap`. That is a verified false
  positive, recorded rather than "fixed" with a wrong `:inherited`.
- [DECISION] The upload meter becomes an **indeterminate busy state**: htmx 4
  sends the body with `fetch()`, which reports no upload progress, and the owner
  chose that over a hand-rolled XHR path or a bundled uploader. `hx-preserve`
  keeps the chosen file across the storage re-render.
- [DECISION] **The component spin ticket 02 deferred.** The tokens take shadcn's
  own names (`--background`, `--primary`, `--border`, `--ring`, ...) so a
  shadcn-htmx component copies in unmodified. `--primary` is **ink**, so every hue
  is a status; light is paper, dark is graphite. The *record* (transcript,
  minutes, draft bodies) is a system **serif** reading column; mono is for data
  only (paths, hashes, `name=value`). The ALL-CAPS tracked eyebrows are gone.
- [DECISION] **A Playwright gate** (`just e2e`) drives the real server against a
  seeded data dir, asserts the flows, and writes light/dark desktop/mobile
  screenshots for visual review. It is not part of `just verify` (it needs bun
  and a browser). It already earned its place: it caught a duplicate
  `#agent-setup` id (the mount and the fragment both carried it) and htmx-only
  `<a>` controls with no link role, which were not keyboard-focusable (now
  `<button>`).
- Accepted trade-offs, all deliberate: no upload percentage; no theme toggle yet,
  so the dark scheme follows `prefers-color-scheme` and shadcn's `.dark` class is
  not wired; the project view still repeats each meeting's full run, tape and
  archive controls, which reads long — collapsing them is a candidate follow-up,
  not taken here because the spin kept the DOM structure.
