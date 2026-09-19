// The console's single JavaScript entry.
//
// htmx and Alpine are real dependencies now (ADR-0023): this file is what
// replaces the hand-committed `htmx.min.js` / `alpine.min.js`. Vite bundles
// them, plus the stylesheet, into `static/app.js` and `static/app.css`.
//
// Nothing here fetches anything at run time — the bundle is served from the
// wheel, so the console stays offline. No CDN, no browser-side compilation.
import htmx from "htmx.org";
import Alpine from "alpinejs";

import "./app.css";

// Importing htmx runs its own DOM-ready processing; keep it reachable as a
// global so a future inline handler can use it, exactly as the vendored script
// tag did.
window.htmx = htmx;

// Alpine does not self-initialize in its bundled ESM build (only the CDN build
// does), so start it explicitly. It waits for the DOM itself.
window.Alpine = Alpine;
Alpine.start();
