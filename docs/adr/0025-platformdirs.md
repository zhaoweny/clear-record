# ADR-0025 — Adopt `platformdirs`; platform-native directories (supersedes ADR-0007's one-layout rule)

Status: active
Date: 2026-09-15

- **Supersedes in part** [ADR-0007](0007-deployment-directories-xdg.md): its
  *"XDG Base Directory spec on every supported platform … for one consistent
  layout"* directive, and the `[OPEN]` it left for *"the macOS fallback and the
  Windows mapping"*. ADR-0007's **other** half stands: the *workspace* (the user's
  recordings and derived record) is **not** app-owned and stays wherever the user
  points.

## Context

- [VOICE: owner, 2026-09-15] The request, verbatim: *"and feature request: adopt
  https://pypi.org/project/platformdirs/"*. Told that this reverses ADR-0007's
  one-layout directive (macOS would move from `~/.local/share/clear-record` to
  `~/Library/Application Support/clear-record`, and existing data would need
  migrating), the owner chose **"Adopt platformdirs native — reverse the
  one-layout rule"**.
- [FACT] `platformdirs` 4.11.8: **MIT**, `requires-python >=3.10`, **zero
  dependencies**, a 24 KB wheel, actively released. It returns platform-native
  paths — the exact alternative ADR-0007 *discarded* in order to have one layout.
- [FACT] ADR-0007 left its macOS and Windows mapping **unresolved** (`[OPEN]`), and
  that is precisely why the codebase only implements the Linux/XDG form: three
  modules hand-roll the same literals — `service/paths.py` (config/data/state),
  `core/diagnostics.py` (state/logs) and a cache reference in `cli/workspace.py`.
- [FACT] ADR-0007's tentative kind-split (`models`/`glossary` → data, chunk cache →
  cache, logs/resume → state) maps cleanly onto `platformdirs`' own categories, and
  so does the managed workspace root just decided (`data`).
- [FACT] **`clear_record.core` may not import a third-party package**
  (`tests/test_layering.py`). The base-directory resolution therefore cannot move
  into `core` as-is.
- [REQ] An existing macOS install must **not** appear to have lost its data.

## Decision

- [DECISION] **Adopt `platformdirs`** as the single resolver for app-owned
  locations, with **platform-native paths**: macOS `~/Library/Application
  Support`, Windows `%APPDATA%`/`%LOCALAPPDATA%`, Linux the XDG dirs. This
  **supersedes ADR-0007's one-consistent-layout directive** and **resolves its
  macOS/Windows `[OPEN]`**.
- [DECISION] The **kind-split stands as ADR-0007 tentatively mapped it**, now
  backed by the library's own categories: **data** (registry, models, glossary,
  managed workspaces), **config** (the TOML), **cache** (the chunk cache), **state
  and logs** (diagnostics).
- [DECISION] **The `CR_*` overrides and the flag > env > config > default
  precedence survive unchanged.** `CR_DATA_DIR`, `CR_LOG_DIR`,
  `CR_WORKSPACE_ROOT`, `CR_MODELS_DIR` etc. remain the escape hatches; a user can
  still pin every location by hand.
- [DECISION] **The workspace is not app-owned** (ADR-0007's surviving half). The
  managed workspace root *defaults* under `data`, and `meeting.workspace_path` and
  `CR_WORKSPACE_ROOT` still win.
- [DECISION] **One resolver, not three.** The three hand-rolled copies collapse into
  a single module, and `core` **receives** its directory rather than computing it —
  because `core` may not import `platformdirs`. The layering rule is not relaxed
  for this; the base-dir lookup moves up to a layer that may import third-party.
- [DECISION] **Migration is part of the change, not a follow-up.** On first run, if
  the **legacy XDG location exists and the native one does not**, the app adopts the
  legacy location and says so in one line, or moves it — the implementing slice
  chooses one and must justify it; an interrupted move must not lose data. The ADR
  records the requirement; the ticket records the mechanism and its test.
- [DECISION] `platformdirs` joins the base distribution's dependencies; the
  packaging guard is widened to say so.

## Rationale

- It replaces three hand-rolled, mutually-drifting copies with a maintained,
  zero-dependency, MIT-licensed implementation, and answers the macOS/Windows
  question ADR-0007 left open.
- The owner accepted the cost of the reversal knowingly: one consistent layout is
  worth less to them than being conventional on each platform.
- Keeping the `CR_*` overrides preserves the "you own the hardware, you choose the
  paths" property that motivated the original config-file override.

## Discarded alternatives

- **Keep XDG everywhere and just unify the three copies** — the smaller change and
  no data movement, but it leaves the macOS/Windows mapping invented rather than
  maintained. Offered, and not chosen.
- **Adopt `platformdirs` pinned to XDG semantics** — uses the library with its
  platform detection disabled, i.e. its main feature switched off.
- **Adopt the library but skip migration** — would make an existing install look
  like it lost its registry and models. Rejected outright.

## Consequences / review hook

- **Paths move on macOS and Windows.** Every documented path in the README, ADRs
  and docs referencing `~/.local/share/clear-record` must be corrected.
- The **layering test is the guard** for the `core` constraint: if the resolver
  sneaks into `core`, CI says so.
- The migration path needs a test with a fake legacy directory.
- Revisit if the migration proves disruptive, or if a future platform wants the
  old layout back via config.
