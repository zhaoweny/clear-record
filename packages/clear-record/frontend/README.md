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
