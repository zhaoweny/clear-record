# ADR-0007 — Deployment directories: XDG Base Directory + a source/user mode

Status: active
Date: 2026-09-11

## Context

- [VOICE] (2026-09-11) "when we deploy: we follow XDG Base Dir spec for all
  supported platform. but we should have a config file to indicate it's source
  build or it's user-mode."
- [FACT] Development and validation runs are **source-only** for now — run from
  the checkout (`uv run`), not from a packaged install.
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
- [DECISION] **A configuration file declares the run mode** — `source` or
  `user`:
  - `source` → persistent data stays **in the checkout** (`models/`,
    `recordings/`): today's behaviour. A checkout-local equivalent for
    XDG-style state is `[OPEN]`.
  - `user` → persistent data lives under the XDG directories above.
  - Proposed location `$XDG_CONFIG_HOME/clear-record/config.toml` (path and keys
    `[OPEN]`).
- [DECISION] The **mode selects where models are looked up and downloaded** (the
  ADR-0005 auto-download target); that target becomes the models-directory
  resolver rather than a hard-coded `<cwd>/models`.
- [DESIGN] Precedence: explicit CLI flags > environment variables (`CR_*`) > the
  config file > built-in defaults.

## Rationale

- One consistent layout across operating systems beats per-OS ad hoc paths, and
  matches what Linux-native users already expect.
- Separating **data / cache / state** lets the chunk cache (written by the
  `whisper-cli` worker pool) be evicted without touching durable models or
  exported recordings.
- The mode switch keeps the **source build clean** (everything in the checkout)
  while giving a real install a conventional place to live, so dev assumptions
  are not baked into the shipped layout.
- A single models-directory resolver lets the `HF_ENDPOINT` mirror override, the
  auto-download, and the mode switch work together.

## Discarded alternatives

- **Platform-native directories** (`~/Library/Application Support` on macOS,
  `%APPDATA%` on Windows) — more "native" per OS, but the owner asked for XDG on
  every platform for one consistent layout.
- **Always XDG, no mode switch** — would move dev artifacts out of the checkout
  and surprise the source-only workflow.
- **Keep `<cwd>/models` everywhere** — no notion of an installed user
  deployment.

## Consequences / review hook

- **Inert in dev for now:** with source-only runs, `source` keeps today's
  behaviour and the XDG layout only activates once the mode config lands.
- The models-directory resolver becomes the single seam the mode config drives;
  the ADR-0005 auto-download path changes only there.
- `[OPEN]` to settle before implementing: auto-detect `source` vs `user` or
  always read the config; the exact config format and keys; the macOS fallback
  and the Windows mapping; which artifacts are `data` vs `cache` vs `state`
  (models and glossary → data, chunk cache → cache, logs and resume state →
  state, tentatively).
- Revisit at the first real packaging/distribution push (mirrors ADR-0004's
  review hook).
