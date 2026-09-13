# ADR-0007 — Deployment directories: XDG Base Directory with a config-file override

Status: active
Date: 2026-09-11

## Context

- [VOICE] (2026-09-11) "when we deploy: we follow XDG Base Dir spec for all
  supported platform. but we should have a config file to indicate it's source
  build or it's user-mode."
- [FACT] Development and validation runs currently execute from the checkout
  (`uv run`), not from a packaged install.
- [DESIGN] The **source build** is expressed by a config that points paths at
  the checkout, not by a separate mode key.
- [FACT] Today the models directory defaults to `<cwd>/models`
  (`_default_models_dir`), the workspace is an arbitrary directory, and the chunk
  cache lives inside that workspace. Environment overrides already exist:
  `CR_MODELS_DIR`, `CR_WHISPER_CLI`, `CR_GGML_BACKEND_DIRS`, `CR_VRAM_GB`,
  `CR_JOBS`.
- [FACT] ADR-0006 keeps recordings, derived transcripts and model weights
  environment-local and gitignored; ADR-0005 auto-downloads `ggml-*.bin` when a
  backend first needs it (with an `HF_ENDPOINT` override for mirrors).
- [FACT] ADR-0004 fixes the uv workspace / src-layout module structure.

## Decision

- [DECISION] **Follow the XDG Base Directory spec on every supported platform**
  for all persistent directories — config, data, cache and state:

  | Kind | Variable | Default |
  |---|---|---|
  | config | `$XDG_CONFIG_HOME` | `~/.config` |
  | data | `$XDG_DATA_HOME` | `~/.local/share` |
  | cache | `$XDG_CACHE_HOME` | `~/.cache` |
  | state | `$XDG_STATE_HOME` | `~/.local/state` |

  Respect the variables when set; when unset, use the spec defaults. The macOS
  fallback and the Windows mapping are `[OPEN]` (see Consequences).
- [DECISION] **No config → the user deployment.** Config, models, cache and
  logs/state resolve under the XDG directories above. There is no source-build
  default.
- [DECISION] **The workspace is not app-owned XDG data.** The `directory`
  argument names a user-chosen workspace holding the source recordings and the
  derived record. Those are the user's documents, kept wherever the user points,
  not `clear-record`'s own data (see ADR-0006). Only config, models, cache and
  logs/state are app-owned under XDG.
- [DECISION] **The supported override is a minimal TOML config file.** When a
  config is present, its paths are used for anything not set by a CLI flag or a
  `CR_*` variable; it may point paths at a checkout (e.g. `models/` and a
  workspace inside the repo), which is how the **source build** is expressed.
  Source build vs user deployment is therefore *not a mode key*: it is simply
  **"no config = user deployment under XDG; a config may point at the
  checkout"**. Proposed location `$XDG_CONFIG_HOME/clear-record/config.toml`;
  the exact keys/schema are `[OPEN]`.
- [DECISION] The config selects where models are looked up and downloaded (the
  ADR-0005 auto-download target); that target becomes the models-directory
  resolver rather than a hard-coded `<cwd>/models`.
- [DESIGN] Precedence: explicit CLI flags > environment variables (`CR_*`) > the
  config file > built-in defaults (the XDG directories).

## Rationale

- One consistent layout across operating systems beats per-OS ad hoc paths, and
  matches what Linux-native users already expect.
- Separating **config / data / cache / state** lets the chunk cache (written by
  the `whisper-cli` worker pool) be evicted without touching durable models or
  config. The user's recordings and derived record live in the user's own
  workspace, outside these app directories, so they are never silently treated
  as evictable app data.
- The config override keeps the **source build clean** (everything in the
  checkout) while giving a real **user deployment** a conventional place to
  live, so dev assumptions are not baked into the shipped layout.
- A single models-directory resolver lets the `HF_ENDPOINT` mirror override, the
  auto-download, and the config override work together.

## Discarded alternatives

- **Platform-native directories** (`~/Library/Application Support` on macOS,
  `%APPDATA%` on Windows) — more "native" per OS, but the owner asked for XDG on
  every platform for one consistent layout.
- **A `mode = source|user` key** — rejected: the presence and contents of the
  config already express source build vs user deployment, so a mode key would
  add a second source of truth that can contradict the paths.
- **Always XDG, no config override** — would move dev artifacts out of the
  checkout and surprise the source build workflow.
- **Keep `<cwd>/models` everywhere** — no notion of an installed user
  deployment.

## Consequences / review hook

- **Default is the user deployment:** with no config, an installed run writes
  under the XDG directories. The source build opts in with a config that points
  at the checkout, so the shipped default is the conventional user layout.
- The models-directory resolver becomes the single seam the config drives; the
  ADR-0005 auto-download path changes only there.
- `[OPEN]` to settle before implementing: the exact keys/schema (TOML is
  decided); the macOS fallback and the Windows mapping; which **app-owned**
  artifacts are `data` vs `cache` vs `state` (models and glossary → data, chunk
  cache → cache, logs and resume state → state, tentatively). Recordings and the
  derived record are **not** app-owned artifacts — they stay in the user's
  workspace wherever the `directory` argument points.
- Revisit at the first real packaging/distribution push (mirrors ADR-0004's
  review hook).

## Update (2026-09-13) — the models-directory resolver exists

- [DESIGN] `cr_providers.paths.resolve_models_dir` is now the single resolver
  for the models directory: an explicit CLI flag (`--models-dir`) →
  `CR_MODELS_DIR` → `<cwd>/models`. The CLI's `--models-dir` default and the
  provider's first-use ggml download both call it, replacing the duplicated
  precedence that lived in `cr_cli.cli._default_models_dir` and
  `cr_providers.backends._resolve_ggml_model`. The config file / XDG defaults
  above land in this one module rather than a third copy.
