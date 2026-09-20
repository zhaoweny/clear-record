# Logging, deployment, i18n, workspace, and paths — owner-voice record (2026-09-15)

Status: In force, except the XDG one-layout rule, superseded by ADR-0025 (annotated in place). Live owners: ADR-0021, ADR-0024, ADR-0025; i18n lives in `docs/i18n.md`.
Moved here verbatim from `docs/vox/voice-of-owner.md` on 2026-09-21: no wording changed — the entry
keeps the standing positions and the index, and each section below keeps its own date.

## User-feedback logging, and running the console as a service (2026-09-15)

- Owner requests, verbatim:
  - *"log system. do logs so actual user can feed back actual logs to us - if
    there are any user."*
  - *"and system-service check - can clear-record's web interface run as a web
    service then? docker or systemd or flatpak service situation, need
    investigation"*
- [REQ] Scoped in the local tracker's `diagnostics` lane (the log system + a
  user-feedback bundle) and `service-deployment` lane (the systemd / Docker /
  Flatpak service investigation).
- [VOICE: owner, 2026-09-15] i18n request, verbatim: *"I'd like to have some i18n,
  as I'd like to have pybabel or something, for all user-facing UI strings. I
  propose a `tr()` shorthand for strings needed to translate"*.
  - [DESIGN] Scoped in the `i18n` lane. The agent proposal is **stdlib `gettext`
    at runtime, Babel at build time** — that keeps `clear_record.core`'s
    no-third-party rule intact (no new runtime dependency at all) while still
    using `pybabel` for extraction and compilation, and reuses the owner's
    established pattern of a **committed compiled artifact plus a CI freshness
    guard** (ADR-0023's `web-assets-check`).
  - [DECISION] `tr()` is the shorthand, message IDs are the English strings, and
    **the English default is provably unchanged** — the same
    byte-identical-default discipline the CLI already carries.
  - [DECISION] A hard boundary: **logs, the JSON API, exports and the diagnostics
    bundle are never translated** — the record is the user's data, in the language
    they spoke.
- [VOICE: owner, 2026-09-15] Managed-workspace request, verbatim: *"clear-record
  managed workspace: the user creates a meeting, then upload tapes to
  clear-record; clear-record manages tapes on behalf of user and do all the
  transcription work - this is a route to self-host and manage clear-record
  remotely"*.
  - [FACT] This **amends ADR-0007 in part**: that ADR decided the workspace is
    *not* app-owned (*"the user's documents, kept wherever the user points, not
    clear-record's own data"*). A **managed** workspace is app-owned, so the
    amendment must be scoped — the CLI's user-chosen `--dir` workspace is
    unchanged, and the managed root is the console's addition.
  - [FACT] It also raises the stakes on **ADR-0021**: every surface before this one
    *read* local files; uploads **write multi-GB files** to a node whose auth is
    the operator's proxy. Recorded in the `managed-workspace` lane.
  - [VOICE: owner, 2026-09-15] Location clarification, verbatim: *"let's say that
    user could use a default workspace under `$XDG_DATA_HOME` or
    `$XDG_STATE_HOME`; but they may also choose a new place where they would save
    the tape."*
  - [DECISION] The default root is **`$XDG_DATA_HOME/clear-record/workspaces/`** —
    **data, not state** (ADR-0007's split: state is the removable-without-losing-
    data bucket; an uploaded tape is its opposite). *(Superseded 2026-09-15 by
    ADR-0025: the root defaults under the platform-native data directory; the
    data-not-state split stands.)* A user-chosen place stays
    first-class at two levels: globally (`CR_WORKSPACE_ROOT`) and **per meeting**
    (`meeting.workspace_path`, which already exists).
  - [DECISION] **`chunks/` should move to `$XDG_CACHE_HOME`** — ADR-0007 already
    says so, and it matters more in a managed workspace: keeping derived cache
    inside the content directory means deleting tapes does not reclaim the cache,
    and a cache sweep would walk the user's data. *(Realized 2026-09-15 as the
    platform-native cache directory — ADR-0025.)*
- [VOICE: owner, 2026-09-15] platformdirs request, verbatim: *"and feature request:
  adopt https://pypi.org/project/platformdirs/"*.
  - [FACT] Shown that this **reverses ADR-0007's one-layout rule** (macOS moves
    from `~/.local/share/clear-record` to `~/Library/Application Support/…`) and
    that existing macOS data would need migrating, the owner chose **"Adopt
    platformdirs native — reverse the one-layout rule"**.
  - [DECISION] **ADR-0025**: adopt `platformdirs` (MIT, zero deps) with
    **platform-native** paths; this **supersedes ADR-0007 in part** and **resolves
    its macOS/Windows `[OPEN]`**. ADR-0007's surviving half stands — the workspace
    is still not app-owned. The kind-split (data / config / cache / state+logs) is
    kept, `CR_*` overrides and precedence survive, `core` stays third-party-free by
    **receiving** its directory, and **migration is part of the change**: an
    existing XDG install is adopted, never orphaned.
- [FACT] Both touch the local-first/privacy stance: logs are where private
  material leaks by accident (file names, glossary terms, transcripts), and a
  *service* exposed beyond localhost collides with ADR-0013's "localhost only, no
  auth".
- [DECISION] On the deployment question the owner chose, verbatim: *"do localhost
  only and let user to do the reverse proxy part"* — **"(for now)"**. Recorded as
  [ADR-0021](../../adr/0021-localhost-only-deployment.md): the app stays on
  `127.0.0.1` with no auth, the operator's reverse proxy owns remote access and
  authentication, and **in-app LAN auth is deferred, not rejected**. The same
  decision closes a gap the investigation exposed — a browser-accessible
  localhost service still needs an `Origin`/`Host` guard against CSRF and DNS
  rebinding — so the app now owes the operator a proxy recipe **and** that guard.
- [VOICE: owner, 2026-09-15] CLI framework suggestion, verbatim: *"perhaps we may
  adopt click for cli interfaces"*. Recorded as an evaluation, not a decision
  (the `cli-framework` lane). Grounding it surfaced the real defect — the
  CLI is argparse but the **`CR_*` environment surface has grown to 21 variables**
  with precedence re-implemented in several modules — so the recommended order is
  to unify the env layer first and treat the Click port as a later, deliberate
  prefactor. (Owner then chose **"Adopt Click now"**; the port landed as `f5be80e`
  and is pushed — see [ADR-0022](../../adr/0022-adopt-click.md).)
- [VOICE: owner, 2026-09-15] Tailscale simplification and UI iteration, verbatim:
  *"can we do a tiny bit of tailscale integration to make it simpler? I'd like to
  for example, `clear-record web --tailscale` to setup the tailscale serve; and
  use something like a parent-child process to handle the environment variable
  setting. on top of that, the web interface should have some design iteration."*
  - [DECISION] `--tailscale` ships; **no child process** — the resolved tailnet
    name is passed straight into `create_app(trusted_hosts=...)`, the explicit
    seam that already existed, so the env var stops being load-bearing (agent
    recommendation, accepted; the `tailscale` lane).
  - [DECISION] Owner follow-up on **ports**: *"it could take same port or different
    port as the server on 127.0.0.1 - I think default to same port, and user may
    choose different ports."*
  - [REQ] Owner named the design reference: **shadcn-htmx**
    (<https://shadcn-htmx.productdevbook.com/>) — shadcn-style components for the
    server, MIT, shipping htmx v4 **+ Tailwind v4** flavours including **Jinja2**
    and raw HTML. Offered "patterns in plain CSS" versus "Tailwind via a
    precompiled artifact", the owner went further, verbatim: *"let's do a proper
    front-end spin; and I guess we could have someone to handle the rebuild of a
    source release, while we ship compiled result by default. this also opens the
    path to some more complicated SPA and web apps"*.
  - [DECISION] **ADR-0023**: a real front-end source tree with a committed
    lockfile and a bundler; compiled assets **committed** and shipped in the wheel
    (so users need no Node); `just` owns build + a **freshness guard**; Tailwind v4
    with shadcn-htmx as the component reference; **no CDN and no browser-side
    compilation**. This **supersedes ADR-0016's "no build step"** while preserving
    its reason — the offline, no-runtime-toolchain guarantee — by shipping compiled
    output.

