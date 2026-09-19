# Front-end assets

How the web console's CSS and JS are built, and what a contributor has to do
when changing the UI. The decision record is
[ADR-0023](adr/0023-frontend-toolchain.md); the source tree has its own
[README](../packages/clear-record/frontend/README.md).

## The shape

- The front-end **source** lives in `packages/clear-record/frontend/`
  (Vite + Tailwind v4, bun as the package manager, `bun.lock` committed).
- The **compiled output** (`app.css`, `app.js`) is written into
  `packages/clear-record/src/clear_record/web/static/` and **committed**. htmx
  and Alpine are bundled in — no CDN, no browser-side compilation, no runtime
  fetch.
- `uv_build` ships `clear_record/web/**` in the wheel, so a `pip install`
  carries the same assets with **no Node on the user's machine**.

## Recipes

```sh
just web-assets          # build (needs bun)
just web-assets-check    # freshness guard: rebuild, fail on any committed diff
```

`just web-assets-check` is the guard that keeps the committed output honest. It
lives in a **separate CI job** (`verify.yml` → `web-assets`), not in
`just verify`:

- The guard needs bun. Folding it into `verify` would block a contributor who
  never touches the UI, and the repo's rule is that the Python gate stays
  runnable without a front-end toolchain.
- CI still enforces freshness on every push and PR, so the committed assets
  cannot drift unnoticed.

## When you change the UI

1. Edit `packages/clear-record/frontend/src/` (and/or the Jinja templates'
   classes under `clear_record/web/templates/`).
2. Run `just web-assets`.
3. Commit the source **and** the rebuilt `web/static/` output together.

No compiled file is ever edited by hand.

## End-to-end and visual review

`just e2e` runs the Playwright suite against the **real** server: `e2e/seed.py`
writes a deterministic data dir, the config's `webServer` boots
`clear-record web` against it, and the specs drive the UI headlessly. It writes
light/dark desktop/mobile screenshots to `.local/e2e/screenshots/` for visual
review, alongside the component gallery that loads the shipped `app.css`.

`just e2e` is **not** part of `just verify`: it needs bun, Python and a
downloaded browser. Before it seeds, boots or launches a spec it runs
`scripts/check_e2e_provisioning.py` — a pointer script, since it branches —
which exits non-zero naming the fix for whichever piece a fresh worktree
lacks: `bun install --frozen-lockfile --cwd packages/clear-record/frontend`
for the dependencies, `just e2e-install` for the browser. Without the guard a
missing dependency dies at `playwright: command not found` (exit 127), and a
missing browser fails once per spec in `browserType.launch` — one line
instead of a diagnosis session.

`just e2e-install` fetches Chromium into `.local/ms-playwright` **per
machine**, not per worktree: the download lands once in the main checkout's
`.local/ms-playwright`, and a worktree shares it only when its own
`.local/ms-playwright` is a symlink there — a link environment provisioning
makes, not a committed step. A worktree without the link downloads its own
copy. Point a run at another location with `PLAYWRIGHT_BROWSERS_PATH`.
