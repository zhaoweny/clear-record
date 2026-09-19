# Console front-end

Source for the web console's compiled assets. The build output lands in
`../src/clear_record/web/static/` and **is committed**, so a source release, a
wheel built on a laptop, and a `pip install` all carry the same bytes. End users
never need Node. See [ADR-0023](../../../docs/adr/0023-frontend-toolchain.md)
and [docs/frontend-assets.md](../../../docs/frontend-assets.md).

## What is here

| Path | What it is |
| --- | --- |
| `package.json` | Dependencies and the `build` script. |
| `bun.lock` | The committed lockfile (bun). Never hand-edited. |
| `vite.config.mjs` | The build: Vite + Tailwind v4, output into the Python package. |
| `src/main.js` | The JS entry. Imports htmx and Alpine and starts them. |
| `src/app.css` | The stylesheet source: the Tailwind theme (`@theme`) and the console's component rules. |
| `node_modules/` | Installed deps (gitignored). |
| `playwright.config.ts` | The e2e run: one booted server, one seeded data dir. |
| `e2e/seed.py` | Seeds a deterministic console (projects, meetings, transcript, glossary). |
| `e2e/*.spec.ts` | Flow tests, visual captures, and the component gallery. |
| `e2e/fixtures/components.html` | The gallery page, loading the shipped `app.css`. |

The interaction model is unchanged: htmx + Alpine over server-rendered Jinja.
htmx and Alpine are real npm dependencies now; they are bundled into
`static/app.js` instead of being hand-committed vendor files. Nothing is fetched
at run time — no CDN, no browser-side compilation.

## Build

Requires [bun](https://bun.sh) (the toolchain the owner chose for workspace-local,
repeatable installs).

```sh
just web-assets          # install (from the lockfile) + build
just web-assets-check    # rebuild and fail if the committed output is stale
```

`just web-assets` writes:

```text
src/clear_record/web/static/app.css
src/clear_record/web/static/app.js
```

Both are committed. Change a source file, run `just web-assets`, and commit the
source **and** the rebuilt output together.

## End-to-end and visual review

```sh
just e2e-install   # one-time per machine: download Chromium into .local/ms-playwright
just e2e           # seed, boot the real console, run the specs, write screenshots
```

`just e2e` is not part of `just verify` — it needs bun and a browser. It writes
screenshots to `.local/e2e/screenshots/` (gitignored) for the visual review, and
its specs assert the htmx wiring those captures show.

The browser download is per **machine** by convention, not per worktree: it
lands once in the main checkout's `.local/ms-playwright`, and a worktree shares
that copy when its own `.local/ms-playwright` is a symlink to it — the link comes
from environment provisioning, not from a committed step. `just e2e` provisions
nothing; it checks first and fails immediately, naming the fix — `bun install
--frozen-lockfile --cwd packages/clear-record/frontend` when the dependencies
are not installed, `just e2e-install` when the browser is not — rather than
letting a fresh worktree die on `playwright: command not found` or one
`browserType.launch` failure per spec. Point a run at another browser copy with
`PLAYWRIGHT_BROWSERS_PATH`.

### Filenames are stable, not content-hashed

`base.html` references literal `/static/app.css` and `/static/app.js`. The
console is a localhost-only tool served from the package, so content-hash
cache-busting buys nothing and would require a manifest plus a Jinja helper
registered in `web/app.py`. Stable names keep the wiring honest and the diff
small. Revisit if the console ever grows long-lived shared caches.

### Adding a dependency

```sh
cd packages/clear-record/frontend
bun add <package>            # runtime: bundled into static/app.js
bun add -d <package>         # build-time only
```

Commit the updated `bun.lock` and the rebuilt assets.

## How it fits the wheel

`uv_build` ships everything under `clear_record/web/` inside the import package,
so the compiled assets ride in the wheel as package data (ADR-0016). The
`packages/clear-record/tests/test_packaging.py` guard checks they exist, and the
PyInstaller spec collects them explicitly.
