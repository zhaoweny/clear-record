# clear-record as a service — systemd, containers, Flatpak, launchd

Status: research complete
Date: 2026-09-15
Lane: the local tracker's `service-deployment` lane, ticket 01
(that tracker lives in the main checkout; this note is the deliverable).

Provenance rule: every claim carries a label (**FACT / VOICE / REQ / DESIGN /
SUGGESTION / OPEN**) and an inline primary-source URL. Anything I could not
verify from the owner of the claim is marked **[OPEN]**. Nothing here is a
decision. **No size, flag or behaviour is invented**, and **nothing was run**:
this note is a reading of primary docs plus the repository's own source.

Sources that are *project trackers* (the Flatpak issue) or *secondary news* are
labelled as such and never promoted to a vendor's own statement.

---

## TL;DR — the three deployment shapes, and my verdict on each

| Shape | Verdict | The honest one-liner |
|---|---|---|
| **systemd (user unit)** | **Works today, no code change** | Run `clear-record web --no-browser` under a user unit; `Type=exec`, `Restart=on-failure`; XDG dirs already resolve from the unit's environment. Secrets are the weak spot: the app reads the webhook secret from **`os.environ`**, and systemd says env is not for secrets. |
| **Docker / Podman** | **Works, but the image is *ours to build*** | The base image has no `whisper-cli` and no ggml backend — the same wall ADR-0015 hit. A working image must **build and bundle `whisper.cpp` with a ggml backend** *inside* the image, then mount workspace/models/XDG as volumes and pass through a GPU (`--gpus`/CDI for NVIDIA, `--device /dev/dri` for Vulkan). |
| **Flatpak as a service** | **No — plainly not supported today** | Flatpak has **no supported background-service model**. The request to export systemd user units has been an **open** enhancement issue since 2019. `flatpak build-finish` exports desktop files, icons, D-Bus services and AppStream metadata — never a systemd unit. |

Two cross-cutting truths:

- **[FACT, repo]** `clear-record web` and `clear-record tray` already default to
  `127.0.0.1:8765`; the console is **localhost-only and has no authentication** by
  design (ADR-0013). Anything that reaches it from another device is a **new
  threat model**, and that is an **owner decision**, framed in §4 — not made here.
- **[FACT, repo]** The always-on node in the architecture is an **Apple Silicon
  Mac mini** (`docs/architecture.md` §5). systemd does not run there; **`launchd`
  does** (§7). The tray is the *desktop* answer (ADR-0016); a headless daemon is
  the *node* answer, and ADR-0013's promised **`serve` subcommand is not built**.

---

## 0. What the console actually is today (the baseline all shapes must preserve)

- **[FACT, repo]** The registered console entry points are `web`, `tray` and
  `mcp`; the console is **not** a separate dist and is not a system service —
  `[project.entry-points."clear_record.commands"]` lists exactly
  `web = clear_record.web:register`, `tray = clear_record.tray:register`,
  `mcp = clear_record.mcp:register`. `[project.scripts]` exposes
  `clear-record = clear_record.cli:main`.
  — `packages/clear-record/pyproject.toml` lines 86–94.
- **[FACT, repo]** `clear-record web` takes `--host` (default `127.0.0.1`),
  `--port` (default `8765`), `--no-browser` and `--data-dir`.
  — `packages/clear-record/src/clear_record/web/__init__.py:18–75`.
- **[FACT, repo]** `clear-record web` starts **uvicorn in the foreground**
  (`serve()`), and the server object is exposed as `app.state.server` so
  `POST /api/shutdown` can set `should_exit`.
  — `packages/clear-record/src/clear_record/web/app.py:701–741`.
- **[FACT, repo]** There is a health endpoint: `GET /api/health` returns
  `{"status": "ok", "registry": "<path>"}`.
  — `packages/clear-record/src/clear_record/web/app.py:463–465`.
  — **Superseded 2026-09-26 (ADR-0033's auth gate):** that route is gone, and the
  paths moved with it — the console answers under `/web/`, the machine API under
  `/api/v1/`, and no old path answers. The credential-free liveness route is
  `GET /health`, answering exactly `{"status": "ok"}` with no registry path — so
  every `/api/health` named later in this note (the container `HEALTHCHECK`
  included) reads as `GET /health`. The gate runs first, so a retired `/api/*`
  path (`/api/health`, `/api/shutdown`, `/api/runs/{id}`) is answered as any
  gated page is — a `303` to `/web/setup` — while `/api/v1/*` alone is the
  machine surface: anonymous there is a `401`, and a **bearer machine token**
  (ADR-0033) satisfies it exactly as a signed-in session does.
- **[FACT, repo]** The **`tray`** entry point supervises the same app from a
  **Qt-free `ServiceController`**: it starts uvicorn on a daemon thread, polls
  `/api/health` via `wait_until_ready`, and stops by setting `should_exit`;
  its docstring says it is "reusable by a future `clear-record serve
  --supervise` on a headless node". — `clear_record/tray/service.py:1–89`.
- **[FACT, repo]** App-owned data resolves by the ADR-0007 precedence:
  explicit arg → `CR_DATA_DIR` → config `[paths] data_dir` → `$XDG_DATA_HOME/clear-record`;
  the SQLite registry is `<data_dir>/registry.sqlite3`.
  — `clear_record/service/paths.py:1–89`.
- **[FACT, repo]** Environment knobs already exist: `CR_DATA_DIR`,
  `CR_MODELS_DIR`, `CR_WHISPER_CLI`, `CR_GGML_BACKEND_DIRS`, `CR_VRAM_GB`,
  `CR_JOBS` (ADR-0007) and `CR_WEBHOOKS` (webhook config).
  — `clear_record/service/paths.py`, `clear_record/service/webhooks.py`.
- **[FACT, repo]** **Webhook secrets are read from the process environment, at
  delivery time**: an endpoint's `secret_env` *names* an environment variable,
  the value comes from `os.environ.get(...)`, and an endpoint that names a
  secret the environment does not set **fails closed** (nothing is sent).
  The secret is never stored in the registry. — `clear_record/service/webhooks.py:18–22,298–310,544–567`.
- **[FACT, repo]** `CR_MODELS_DIR` today resolves explicit `--models-dir` →
  `CR_MODELS_DIR` → **`<cwd>/models`**; the XDG/config default "land[s] in this
  one module rather than a third copy" (ADR-0007 Update, 2026-09-13). A service
  that does not set the working directory therefore puts models at a
  cwd-dependent path — see §1. — `docs/adr/0007-deployment-directories-xdg.md:106–114`.
- **[FACT, repo]** The workspace (source recordings + derived record) is a
  **user-chosen, non-app-owned** directory per meeting; only config/data/cache/state
  are app-owned XDG data (ADR-0007, ADR-0006). Any service must **not** relocate it.

---

## 1. systemd

### 1.1 User unit vs system unit

**[FACT]** `systemd` runs a **system manager (PID 1)** and, for each user,
**user manager instances** started as `user@<UID>.service`. User units live under
the user's own search path and are distinct from system units.
— systemd, `user@.service(5)`:
<https://raw.githubusercontent.com/systemd/systemd/main/man/user@.service.xml>

**[FACT]** A user manager is normally tied to login. `loginctl enable-linger`
"enable[s] user lingering"; when enabled, "a user manager is spawned for the user
**at boot** and **kept around after logouts**. This allows users who are not
logged in to run long-running services."
— systemd, `loginctl(1)`:
<https://raw.githubusercontent.com/systemd/systemd/main/man/loginctl.xml>

**[FACT]** `User=` semantics differ: for **system** services the default user is
`root` and `User=` may select another; for **user** services of a non-root user,
"switching user identity is not permitted, hence the only valid setting is the
same user the user's service manager is running as."
— systemd, `systemd.exec(5)`:
<https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.exec.xml>

**[DESIGN]** Consequences for clear-record:

- A **system unit** (PID 1, root by default) is the wrong default: it would run
  the console as `root`, and the workspace/models/data would belong to root. If a
  system unit is wanted, set `User=`/`Group=` explicitly and point `CR_DATA_DIR`
  and the workspace at that user's paths.
- A **user unit** matches the app's model (per-user XDG data, per-user models,
  user-owned workspace). It needs `enable-linger=yes` on a headless always-on
  node, or it dies with the login session.
- **[FACT]** `dynamicUser=yes` exists but is unsuitable here: dynamic users are
  allocated per start and recycled, and systemd warns not to leave files behind
  owned by them — fatal for a persistent registry + model cache.
  — `systemd.exec(5)`, *User/Group Identity* (same URL).

### 1.2 `Type=` and `Restart=`

**[FACT]** `Type=exec` is "the better choice" for long-running services and is
"recommended"; unlike `simple`, `systemctl start` **reports failure when the
binary cannot be invoked**, and follow-up units wait until `execve()` succeeded.
`Type=notify` additionally requires the program to call `sd_notify()` with
`READY=1`. — systemd, `systemd.service(5)`:
<https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.service.xml>

**[FACT]** `Restart=` defaults to **`no`**; it takes `no`, `on-success`,
`on-failure`, `on-abnormal`, `on-watchdog`, `on-abort`, `always`. `on-success`
restarts only on a clean exit (exit 0, or `SIGHUP`/`SIGINT`/`SIGTERM`/`SIGPIPE`);
a stop performed by systemd itself does **not** trigger a restart. `RestartSec=`
defaults to **100 ms**; `RestartSteps=` gives exponential backoff.
— `systemd.service(5)` (same URL).

**[DESIGN/SUGGESTION]** For a console daemon: `Type=exec` + `Restart=on-failure`
+ a `RestartSec` that avoids a hot loop. `Restart=on-failure` deliberately does
**not** fight a `systemctl stop` (uvicorn's clean SIGTERM exit is a "success"),
whereas `Restart=always` would resurrect it. `Type=notify` would be nicer but
depends on uvicorn sending `sd_notify`; **whether it does is `[OPEN]`** (I did
not verify it). If readiness matters, poll the documented `GET /api/health` from
an `ExecStartPost=` — `ExecStartPost=` runs after start and takes
`Before=`/`After=` ordering into account (`systemd.service(5)`).

### 1.3 Where the workspace, models and XDG dirs live

**[FACT]** `StateDirectory=`, `CacheDirectory=`, `LogsDirectory=` and
`ConfigurationDirectory=` create directories **relative to the unit type** and
export their paths via `$STATE_DIRECTORY`, `$CACHE_DIRECTORY`,
`$LOGS_DIRECTORY`, `$CONFIGURATION_DIRECTORY`. For **user** units they land under
`$XDG_STATE_HOME`, `$XDG_CACHE_HOME`, `$XDG_STATE_HOME/log/` and
`$XDG_CONFIG_HOME` respectively; for **system** units they land under
`/var/lib/`, `/var/cache/`, `/var/log/` and `/etc/`. They are **not** removed when
the unit stops (except run-time dirs), and they imply `BindPaths=`.
— `systemd.exec(5)` (same URL).

**[FACT]** `WorkingDirectory=` defaults to **root `/`** for the system manager and
to **the user's home directory** for a user manager; `~` means the `User=` home;
`%h` expands to the user's home in directives that support specifiers.
— `systemd.exec(5)` (same URL).

**[DESIGN]** These two facts dovetail with ADR-0007's unresolved default in
§0: a **user unit** starts with cwd = `$HOME`, so `resolve_models_dir`'s
`<cwd>/models` fallback becomes `$HOME/models` unless the unit sets
`CR_MODELS_DIR` (or `WorkingDirectory=`). Setting `CR_MODELS_DIR` (and, if
desired, `CR_DATA_DIR`) explicitly is the robust choice. The **workspace stays
outside** — point meetings at the user's own directories; do not move the
recordings under XDG.
**[OPEN]** ADR-0007 lists the macOS fallback and the exact app-owned
data/cache/state split as unresolved; this note does not settle them.

### 1.4 `CR_*` env and `secret_env` in a unit — and where secrets *should* live

**[FACT]** `Environment=` sets variables for the unit's processes;
`EnvironmentFile=` reads them from a UTF-8 file, and **settings from an
`EnvironmentFile=` override `Environment=`**. A missing environment file fails
the start unless the path is prefixed with `-`.
— `systemd.exec(5)` (same URL).

**[FACT]** systemd explicitly says environment variables are **"not suitable for
passing secrets"**: unit environment "are exposed to unprivileged clients via
D-Bus IPC … and might leak to processes that should not have access". It directs
callers to `LoadCredential=`/`LoadCredentialEncrypted=`/`SetCredentialEncrypted=`.
— `systemd.exec(5)` (same URL).

**[FACT]** `LoadCredential=ID:PATH` passes data via a **read-only file** at
`$CREDENTIALS_DIRECTORY/ID`; the search path for a **per-user** manager is
`$XDG_CONFIG_HOME/credstore/`, `$XDG_RUNTIME_DIR/credstore/`,
`$HOME/.local/lib/credstore/`; the accumulated limit is **1 MB per unit**.
— `systemd.exec(5)` (same URL).

**[REQ]** These two facts collide with the app's current design: the webhook
secret must be a **process environment variable** because
`WebhookEmitter._deliver` reads `os.environ.get(endpoint.secret_env)`. Therefore
`EnvironmentFile=` is the only mechanism that works **without a code change** —
and systemd says env is not a secret store. Options, in order of honesty:

1. **[SUGGESTION, no code change]** `EnvironmentFile=%h/.config/clear-record/webhooks.env`
   with the file mode `0600`, owned by the service user, and the unit additionally
   `NoNewPrivileges=yes`. This works today but the secret is still in the
   process environment (D-Bus-readable). Acceptable on a single-user node;
   **not** a hard secret boundary.
2. **[SUGGESTION, small code change]** Add a `clear_record` path that reads a
   secret from `$CREDENTIALS_DIRECTORY` when set, so `LoadCredential=` can be used
   as systemd intends. This is a new capability and would need its own review; it
   is *not* built.
3. **[SUGGESTION]** Keep the signing secret out of the service entirely by
   putting the HMAC verification on the receiver side only and not signing
   outbound events — but the code currently fails closed when `secret_env` is
   named and unset, so this is a user configuration choice, not a code change.

**[OPEN]** Whether uvicorn/systemd integration would ever let `Type=notify` work
(§1.2), and whether the owner wants a `clear-record` credential-reading seam.

### 1.5 Journal vs our own log files

**[FACT]** The service manager connects a unit's stdout/stderr to the **journal**
by default: `StandardOutput=` defaults to `DefaultStandardOutput=`, "which
defaults to `journal`", and `systemd-journald.service` itself says "The systemd
service manager invokes all service processes with standard output and standard
error connected to the journal by default." Alternatives include
`file:`/`append:`/`truncate:<path>` and `null`.
— `systemd.exec(5)` and `systemd-journald.service(8)`:
<https://raw.githubusercontent.com/systemd/systemd/main/man/systemd-journald.service.xml>

**[FACT]** The journal is **persistent only if `/var/log/journal` exists**;
otherwise it is volatile under `/run/log/journal/` and **lost at reboot**
(persistent storage is chosen when `/var/log/journal/` exists, or forced with
`Storage=` in `journald.conf`).
— `systemd-journald.service(8)` (same URL).

**[DESIGN/SUGGESTION]** A user unit's journal is per-user and works without
root. Because journal persistence depends on a directory outside the unit, a
node that must survive reboots with logs intact should either ensure
`/var/log/journal` exists or set `StandardOutput=append:<path>` under the
app's own `state`/`logs` directory (ADR-0007's tentative "logs and resume state →
state"). The pipeline already writes its **own** progress files inside the
workspace (`<dir>/transcribe.log`, ADR-0005/§8) — those are user-visible
artifacts and are **not** a substitute for service logs.

### 1.6 A service has no tray — reconciling with ADR-0016

**[FACT, repo]** ADR-0016 makes the **PySide6 tray** the native entry point: it
"supervises the console from a system-tray icon (open / status / quit)" and its
logic is a Qt-free `ServiceController`. — `docs/adr/0016-app-shell-htmx-tray-pi-agent.md:53–58`.

**[FACT, repo]** A headless service has no display and no session tray; importing
PySide6 in a daemon would be both useless and heavy, and the `tray` entry point
requires `PySide6` (the `tray` extra).
— `clear_record/tray/__init__.py:24–44`.

**[DESIGN]** The reconciliation is a **two-surface split**, already latent in the
code and the architecture:

- **Desktop:** `clear-record tray` — the tray *is* the supervisor; it starts the
  console in-process and provides the human-facing entry point (ADR-0016).
- **Node:** a **daemon** supervised by **systemd/launchd**, not the tray.
  `ServiceController`'s own docstring anticipates exactly this ("reusable by a
  future `clear-record serve --supervise` on a headless node"). Today the
  registrable command is `clear-record web --no-browser`; the promised `serve`
  subcommand is **not built** (§7).

### 1.7 Illustrative user unit (only to make the above concrete)

**[DESIGN, illustrative — not a committed artifact, directives each sourced above.]**

```ini
# ~/.config/systemd/user/clear-record.service
[Unit]
Description=clear-record console (headless)

[Service]
Type=exec
# %h = the user's home (documented in the EnvironmentFile= entry of systemd.exec(5))
ExecStart=%h/.local/bin/clear-record web --no-browser --host 127.0.0.1 --port 8765
WorkingDirectory=%h
Environment=CR_DATA_DIR=%h/.local/share/clear-record
EnvironmentFile=%h/.config/clear-record/webhooks.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable with `systemctl --user enable --now clear-record.service`, and
`loginctl enable-linger "$USER"` on a node that must run without a login.

---

## 2. Docker / Podman

### 2.1 The wall is the same one Flatpak hit — but a container can escape it

**[FACT, repo]** ADR-0015 records the wall directly: "the transcription path
drive[s] the **system `whisper-cli`** plus a ggml GPU plugin; … A Flatpak sandbox
contains the runtime's filesystem, so the host's `whisper-cli` and `ggml-*.so`
plugin are **not visible**." — `docs/adr/0015-flatpak-linux-distribution.md:11–14`.

**[FACT]** A container image has the same property: it contains only what you
put in it. Unlike Flatpak, a container image **can** build and carry its own
vendored `whisper.cpp` + ggml backend beside the MIT app. The upstream project
already publishes such images, and the build flags are documented:
`cmake -B build -DGGML_CUDA=1` (NVIDIA), `-DGGML_VULKAN=1` (cross-vendor),
`-DGGML_HIP=1` (AMD ROCm).
— ggml-org/whisper.cpp README:
<https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/README.md>

**[FACT]** whisper.cpp's own images are `ghcr.io/ggml-org/whisper.cpp:main`
(`linux/amd64`, `linux/arm64`), `:main-cuda` (**`linux/amd64` only**),
`:main-musa`, `:main-vulkan` (**`linux/amd64` only**).
— whisper.cpp README, *Docker → Images* (same URL).

**[REQ]** A clear-record image is therefore a **two-part image**: the
`clear-record[web]` wheel **plus** a `whisper-cli` and a ggml backend, built in
the image (or copied from an upstream image). The app drives `whisper-cli` as a
subprocess (`clear_record.providers`, ADR-0005), so the binary must be on `PATH`
or `CR_WHISPER_CLI` must point at it, and the plugin must be in a directory the
probe searches (or `CR_GGML_BACKEND_DIRS` must name it).

**[OPEN]** Realistic image size. I did not measure one and will not invent a
number. The dominating components are the base runtime plus the ggml backend
toolkit (a CUDA toolchain/runtime is the large case; a Vulkan or CPU build is
smaller), the Python wheel and its deps. **Model weights must not be in the
image** (AGENTS hard rule; ADR-0006/ADR-0014/ADR-0015): whisper.cpp's own table
puts a single model at 75 MiB (`tiny`) to 2.9 GiB (`large`) on disk.
— whisper.cpp README, *Memory usage* (same URL).

### 2.2 GPU access

**[FACT] NVIDIA (CUDA):** Docker exposes GPUs with `--gpus`
(`--gpus all`, `--gpus device=0`, `--gpus '"device=0,2"'`), and this requires
the **NVIDIA Container Toolkit**. Capabilities can be set with
`--gpus 'all,capabilities=utility'`. — Docker, *GPU access*:
<https://docs.docker.com/engine/containers/gpu/>

**[FACT]** The NVIDIA Container Toolkit is installed via the distro repos
(`apt`/`dnf`/`zypper`) and configured with
`sudo nvidia-ctk runtime configure --runtime=docker` followed by
`sudo systemctl restart docker`; a **rootless** Docker uses
`nvidia-ctk runtime configure --runtime=docker --config=$HOME/.config/docker/daemon.json`
and `systemctl --user restart docker`. For **Podman, NVIDIA "recommends using
CDI"**. — NVIDIA, *Installing the NVIDIA Container Toolkit*:
<https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>

**[FACT] Podman:** `podman run --gpus=ENTRY` (`all` or a vendor identifier)
starts a container with GPU support; `--device=HOST-DEVICE[:CONTAINER-DEVICE][:PERMISSIONS]`
adds a specific device; in rootless mode the device is bind-mounted and
`--group-add keep-groups` may be needed when access is via a supplementary group.
— Podman, `podman-run(1)`:
<https://docs.podman.io/en/latest/markdown/podman-run.1.html>

**[FACT] Vulkan (AMD/Intel/NVIDIA via one binary):** Docker's device passthrough
is the `--device` flag on `docker run` (Docker docs index it under *device*);
Podman documents `--device=/dev/dri`, and the same bind-mount caveat applies.
— Docker, `docker run --device`: <https://docs.docker.com/reference/cli/docker/container/run/#device>;
Podman, `podman-run(1)` above.

**[DESIGN]** ADR-0015 already chose **Vulkan** as the one plausible Linux GPU
backend for a bundle (`-DGGML_VULKAN=ON`, `--device=dri`); the same logic holds
inside a container: Vulkan is one binary for AMD/Intel/NVIDIA, CUDA is a much
larger NVIDIA-only payload. `docs/architecture.md` §5 names an **AMD RX 7900 XTX**
worker, so the container story's first-class GPU is Vulkan/`/dev/dri`, with
NVIDIA/CUDA via `--gpus`/CDI as the second.

### 2.3 Volumes: workspace, models, XDG

**[FACT]** Docker **volumes** are managed by Docker (stored under the host,
e.g. `/var/lib/docker/volumes/<name>/_data`), persist across container removal,
and are "not a good choice if you need to access the files from the host" —
**bind mounts** are for host-visible files. Volumes are mounted with
`--mount type=volume,src=…,dst=…` or `-v name:path`, and can be read-only.
— Docker, *Volumes*: <https://docs.docker.com/engine/storage/volumes/>

**[DESIGN]** Map the three ownership classes explicitly:

| Data | Class | Container mount |
|---|---|---|
| User **workspace** (recordings + derived record) | **User document** (ADR-0006/0007) | **bind mount** a host directory (must be host-visible) |
| **Models** (`CR_MODELS_DIR`) | app-owned, gitignored, downloaded (ADR-0005) | **volume** or bind mount; never baked into the image |
| **XDG** config/data/cache/state (`CR_DATA_DIR`, registry, chunk cache) | app-owned (ADR-0007) | **volume(s)**; the registry + resumable chunk cache must survive restarts |

Because the workspace is the user's own document and the registry is app data,
the clean split is one **bind mount for the workspace** and one **named volume
for the app data dir** (plus a models volume).

### 2.4 Non-root, healthcheck, egress

- **[FACT] Non-root:** the Dockerfile `USER` instruction "sets the user name (or
  UID) … to use as the default user and group for … `RUN` instructions and at
  runtime"; Docker's own Python guide creates an unprivileged `appuser`
  (UID 10001) and switches to it. — Docker, *Dockerfile reference → USER*:
  <https://docs.docker.com/reference/dockerfile/#user>; Docker, *Containerize a
  generative AI application* (appuser example):
  <https://docs.docker.com/guides/genai-pdf-bot/>
- **[FACT] Healthcheck:** `HEALTHCHECK [OPTIONS] CMD …` runs inside the container
  to "detect cases such as a web server stuck in an infinite loop", with
  `--interval`, `--timeout`, `--start-period` and `--retries` (defaults `30s`,
  `30s`, `0s`, `3`). — Docker, *Dockerfile reference → HEALTHCHECK*:
  <https://docs.docker.com/reference/dockerfile/#healthcheck>. The app's health
  endpoint is `GET /api/health` (§0), so the probe can be a stdlib HTTP GET.
- **[FACT] Podman health:** `--health-cmd`, `--health-interval`, `--health-retries`,
  `--health-start-period`, and `--health-on-failure=kill|restart|stop|none`
  (default `none`; Podman notes `kill` "integrates best with systemd"). Podman
  logs to **`journald` by default** (`--log-driver`), with `k8s-file`, `none`,
  etc. available. — `podman-run(1)` (URL above).
- **[FACT] Egress (webhooks):** containers get networking by default; outbound
  HTTP(S) for webhook delivery needs no special grant (the webhook emitter uses
  `urllib.request` from the app). An explicit `--network none` would break both
  first-use model download and webhooks. — Podman, `podman-run(1)` `--network`
  (URL above). **[OPEN]** Whether a hardened deploy wants an egress allow-list;
  not addressed by the docs above.
- **[FACT] Secrets in containers:** Docker/Podman offer environment files
  (`--env-file`, Quadlet `EnvironmentFile=`), and Podman has `--secret`
  (`Secret=` in Quadlet) which mounts a secret as a file/env. The app's
  `secret_env` reads the environment, so the **same caveat as §1.4 applies**.
  — Podman, `podman-run(1)` `--env-file`/`--secret`; Quadlet `podman-systemd.unit(5)`
  below.

### 2.5 Running it under systemd (Podman Quadlet) — and Docker's equivalent

**[FACT]** Podman ships a **systemd generator (Quadlet)**: `.container` files in
the search paths become `.service` units; "Both system and user systemd units are
supported", and the file's `[Service]`/`[Install]` sections pass through to
systemd. Quadlet defaults `.container` `Type=notify`, sets
`WantedBy=default.target` for user units, and warns that pulling/building an image
can exceed systemd's default 90 s start timeout (extend with `TimeoutStartSec=`).
— Podman, `podman-systemd.unit(5)`:
<https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html>

**[FACT]** Quadlet keys map to `podman run` flags, including `Image=`,
`Exec=`, `Volume=`, `Environment=`/`EnvironmentFile=`, `PublishPort=`,
`User=`/`Group=`, `AddDevice=/dev/…`, `HealthCmd=`/`HealthOnFailure=`,
`LogDriver=`/`LogOpt=`, and `AddCapability=`/`DropCapability=`.
— `podman-systemd.unit(5)` (same URL).

**[DESIGN/SUGGESTION]** For a Linux node, a **rootless Quadlet** is the smallest
honest container service: `$XDG_CONFIG_HOME/containers/systemd/clear-record.container`
with `[Install] WantedBy=default.target`, a `Volume=` for each of workspace,
models and app data, `AddDevice=/dev/dri` for Vulkan (or `PodmanArgs=--gpus all`
for NVIDIA), `PublishPort=127.0.0.1:8765:8765` (localhost-only unless §4 is
decided otherwise), and `HealthCmd=`. For **Docker**, the equivalent supervision
is a normal systemd unit whose `ExecStart=docker run --rm …` plus Docker's
`--restart` policy, or Docker Compose as a unit — **[OPEN]** I did not fetch the
Docker `--restart` reference for this note.

---

## 3. Flatpak as a service — the plain answer is **no**

**[FACT]** Flatpak apps are sandboxed and are launched with `flatpak run` ("Run
an application or open a shell in a runtime"); the command set has
`flatpak install/update/run/kill/ps`, and **there is no "install a service" /
"export a unit" command**. — Flatpak, *Flatpak Command Reference* (v1.18.2):
<https://docs.flatpak.org/en/latest/flatpak-command-reference.html>

**[FACT]** `flatpak build-finish` enumerates exactly what is exportable: "desktop
files, icons, **D-Bus service files**, and AppStream metainfo files from the
`files` subdirectory are copied to a new `export` subdirectory." systemd units are
not in that list. — Flatpak, *flatpak-build-finish(1)* (same URL).

**[FACT]** The capability has been requested and is **still open**: Flatpak issue
**#2787, "Consider exporting systemd user units"** (opened by matthiasclasen,
2019-03-26, label `enhancement`, state **Open**), which notes that apps'
daemons "run as systemd user services" and that systemd looks for these in
`~/.config/systemd/user`.
— Flatpak project issue tracker:
<https://github.com/flatpak/flatpak/issues/2787>

**[SUGGESTION]** Therefore:

- There is **no supported Flatpak background-service model today.** Saying
  otherwise would imply parity that does not exist.
- You *can* hack a systemd **user unit** whose `ExecStart=flatpak run
  <app-id>`, but that is an unsupported composition, not a Flatpak feature, and
  it inherits the sandbox wall (host `whisper-cli`/ggml invisible, ADR-0015) plus
  the need for the sandbox to have a session/bus to run at all. **`[OPEN]`**
  whether a `flatpak run` in a headless user unit even starts cleanly; the
  project's open issue is evidence it is not a designed path.
- **Do not imply parity**: Flatpak's role here is the **desktop bundle** story
  (ADR-0015), not the node/service story. If the owner wants a Linux service,
  the container and plain-systemd routes are the candidates.

**[OPEN]** The direction of travel (news reports that a future Flatpak may depend
on a `systemd-appd` permissions component) is **secondary reporting**, not a
Flatpak guarantee of app services; I did not verify it from a Flatpak primary
source and do not rely on it.

---

## 4. The listening / auth decision — framed, not made

**[FACT, repo]** The console binds `127.0.0.1` by default and "has no
authentication — it is a local-first tool, not a hosted service (ADR-0013)"; the
local-only posture is repeated as a hard rule in ADR-0014 ("it must never be
exposed beyond localhost").
— `clear_record/web/app.py:14–16`; `docs/adr/0014-desktop-app-distribution.md:97–98`.

**[REQ]** A service reachable from another device is a **different threat
model**. The three options, with consequences:

### (a) Stay localhost-only; remote access is a tunnel/VPN concern

**[FACT]** `ssh -L [bind_address:]port:host:hostport` forwards a local port over
an SSH channel; the bind address controls who can connect locally, and the
mechanism is the documented "secure connection … going through firewalls".
— OpenBSD, `ssh(1)`:
<https://man.openbsd.org/ssh.1>

**[FACT]** Tailscale Serve "lets you share a local service securely within your
tailnet" and proxies to a **loopback** target ("only `http://127.0.0.1` is
supported for proxies"); with `-bg` it survives reboot, and the tailnet is
authenticated by Tailscale.
— Tailscale, *tailscale serve*:
<https://tailscale.com/kb/1242/tailscale-serve>

- **Cost:** ~zero code; the console keeps its exact posture; every remote client
  needs the tunnel/VPN.
- **Effect on ADR-0013:** **preserved unchanged.**
- **Implication:** the shortest path to "use it from the laptop/phone" without
  touching the security model.

### (b) Bind to the LAN and add real authentication in our code

- **Cost:** security-critical code — login/session or token issuance, credential
  storage, route-by-route enforcement, and CSRF protection for the htmx/Alpine
  **form posts** (the `/ui/*` POSTs are state-changing). Plus tests and a threat
  model that ADR-0013 deliberately avoided.
- **Effect on ADR-0013:** this **supersedes/amends** "no auth"; the ADR must be
  rewritten, not reinterpreted. ADR-0014's "never beyond localhost" is also
  contradicted.
- **Implication:** the app becomes a small multi-user-like service with a
  user/secret lifecycle; the biggest scope increase of the three.

### (c) Bind to the LAN and delegate auth to a reverse proxy

**[FACT]** Reverse proxies provide auth in front of a backend: nginx's
`auth_basic`/`auth_basic_user_file` "limit[s] access to resources by validating
the user name and password"; Caddy's `forward_auth` proxies an auth check to an
external gateway (e.g. Authelia) and continues only on a `2xx`.
— nginx, `ngx_http_auth_basic_module`:
<https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html>;
Caddy, `forward_auth`:
<https://caddyserver.com/docs/caddyfile/directives/forward_auth>

- **Cost:** a proxy config, TLS (if the LAN is not trusted), and an operational
  invariant that the app's port is reachable **only** through the proxy. The app
  itself still has no auth, so a direct hit on `:8765` bypasses everything.
- **Effect on ADR-0013:** the app's code remains localhost-only/no-auth, but the
  **deployment** is no longer localhost-only; ADR-0013 should carry an explicit
  annotation that this is the supported remote shape and that the proxy is the
  only trusted ingress.
- **Implication:** best boundary-per-effort for a single owner, at the price of a
  second moving part and a firewall/port-binding discipline.

**[OPEN / owner decision]** Which, if any, of (a)–(c) is chosen. This note does
**not** decide it. [SUGGESTION] The cost ordering is clearly (a) < (c) < (b).

---

## 5. State durability — what a restart loses, and what to persist

**[FACT, repo]** `RunManager` keeps the live state **in memory**:
`self._states: dict[int, RunState]`, where `RunState` holds `status` and a
Python `list[JobEvent]`; the event API reads that list
(`events_since(index)` = the SSE cursor). Nothing persists the events.
— `clear_record/service/runs.py:90–119,129–163`.

**[FACT, repo]** The **run row does persist** in SQLite: `start()` calls
`registry.create_run(...)`, and `_execute` updates the row through
`running → done|failed` with `started_at`/`ended_at`/`error`/`progress`.
— `clear_record/service/runs.py:166–199,259–318`.

**[FACT, repo]** The consequence after a restart is concrete:

- `GET /api/runs/{id}` returns **404** unless both the run row *and* the
  in-memory state exist (`if run is None or state is None: 404`).
  — `web/app.py:638–644`.
- `GET /api/runs/{id}/events` returns **404** (it requires in-memory state).
  — `web/app.py:646–656`.
- `GET /ui/runs/{id}` returns **404**. — `web/app.py:454–460`.
- **Superseded 2026-09-26 (ADR-0033's auth gate):** the routes above moved with
  the console and the API — the three read here become
  `GET /api/v1/runs/{run_id}`, `GET /api/v1/runs/{run_id}/events` and
  `GET /web/ui/runs/{run_id}`, and `POST /api/shutdown` (named above) becomes
  `POST /api/v1/shutdown` for a script or `POST /web/ui/shutdown` for the
  console. The verdicts this list states are unchanged: the run row persists,
  while the event stream and the live state do not.
- The **meeting row falls back to the last run row**, so the console renders the
  stale status (e.g. `running`) with `polling: False`, no stage and no progress.
  — `web/app.py:144–164,191–210`.
- The "one active run per meeting" guard is **also in memory**
  (`active_state` scans `_states`), so after a restart a **second run can be
  started while the old row still says `running`**.
  — `runs.py:155–163,174–175`.

**[FACT, repo]** What *does* survive and make resumption cheap: ingestion is
idempotent; long tapes run **chunked and resumable** (`<dir>/chunks/<source>/`,
progress in `<dir>/transcribe.log`); Ctrl-C leaves "a consistent, resumable
cache". — `docs/architecture.md` §8.

**[REQ] What a service must persist (my read of the gap):**

- **Runs and their terminal state, reconciled at startup.** On boot, any row left
  `queued`/`running` whose process is gone should transition to an explicit
  **`interrupted`** state (or `failed` with a stable reason), not stay
  misleadingly `running`. This is a small registry change.
- **The event stream, or at least a durable cursor.** Today the stream is a
  process-local list; persisting events (or a monotonic sequence + bounded ring)
  would let the live view reconnect after a restart. **[OPEN]** Whether the owner
  wants full history or only "last N events + final status".
- **The active-run guard.** `active_state` must be derived from the registry
  (persisted status), not only from `_states`, or restarts break the invariant.
- **The queue (§6), if one is added.**

**[SUGGESTION]** The user-facing shape of an interrupted run should be:
status **`interrupted`**, a reason ("service restarted"), and a **Resume**
affordance that re-runs the pipeline into the same workspace — which is exactly
what the resumable chunk cache is for. It should **never** look like a live run
with a stalled progress bar.

---

## 6. Concurrency — the minimal honest queue

**[FACT, repo]** Today there is **no global queue**: `RunManager` enforces only
"one active run per meeting" (`active_state`), and each `start()` spawns its own
daemon thread immediately. — `clear_record/service/runs.py:122–199`.

**[FACT, repo]** Within one run, the GPU is fed by a **bounded worker pool**
(`--jobs`/`CR_JOBS`), sized against the model and detected VRAM, with
`BackendInfo.parallelizable` serializing any backend that is not
process-isolated. — `docs/architecture.md` §8; ADR-0005's design point is 4
concurrent `whisper-cli` processes.

**[FACT, repo]** The compute target is heterogeneous, but **one node = one GPU**
(the Mac mini/Metal, the AMD box, or an NVIDIA box). Two concurrent runs on one
node would each launch their own `whisper-cli` worker pool and **oversubscribe
the same GPU**.

**[DESIGN/SUGGESTION] The minimal honest queue for one node:**

1. **A single FIFO of run requests**, persisted in the registry with a status
   (`queued → running → done|failed|interrupted`) and an enqueue time.
2. **One run executing at a time per node** — honest for one GPU. If the owner
   later has mixed backends (e.g. CPU + GPU), concurrency becomes a policy knob;
   not now.
3. **Keep the per-meeting dedupe** ("do not transcribe the same tape set twice
   concurrently") on top of the global FIFO.
4. **Expose queue position** in the live view; a queued run is *not* running.
5. **Reconcile at startup** (§5): queued/running → interrupted, then let the
   owner re-enqueue or auto-resume.

**[OPEN]** Whether the owner wants more than one worker slot on the AMD/NVIDIA
box once its own VRAM-aware pool already saturates the GPU — the pool already
parallelizes *within* a run, so a second concurrent run is unlikely to help.

---

## 7. The always-on node — `launchd`, and the missing `serve`

**[FACT, repo]** The architecture's always-on node is an **Apple Silicon Mac
mini** running Metal. — `docs/architecture.md` §5.

**[FACT, repo]** ADR-0013 promised a `serve` subcommand in the service member;
what is **registered today** is `web`, `tray`, `mcp` (plus the `clear-record`
script). — `docs/adr/0013-bundled-web-and-service-surface.md:53–58`;
`packages/clear-record/pyproject.toml:86–94`. `ServiceController`'s docstring
still calls `clear-record serve --supervise` a *future* thing.
— `clear_record/tray/service.py:3–6`.

**[FACT] launchd is the macOS supervisor.** Apple: per-user background processes
are **user agents**, "specific to a given logged-in user and executes only while
that user is logged in"; daemons go in `/Library/LaunchDaemons`, agents in
`/Library/LaunchAgents` or the user's `~/Library/LaunchAgents`. On logout the
per-user `launchd` "sends a `SIGTERM` signal to all of the user agents that it
started."
— Apple, *Daemons and Services Programming Guide → Creating Launch Daemons and
Agents*:
<https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html>

**[FACT]** The property list uses `Label`, `ProgramArguments`, and `KeepAlive`
("whether your daemon launches on-demand or must always be running");
`StandardOutPath`/`StandardErrorPath` send stdout/stderr to files;
`WorkingDirectory`, `UserName`/`GroupName` are set **in the plist**, and Apple
warns that processes managed by launchd **must not daemonize** (`fork`+`exec`/
`exit` makes launchd think the process died).
— Apple (same URL).

**[DESIGN]** The macOS node's shape follows directly:

- **LaunchAgent** = the per-user equivalent of a systemd **user unit**; it runs
  only while that user is logged in (so an always-on headless Mac mini needs
  auto-login or a LaunchDaemon).
- **LaunchDaemon** = runs at boot regardless of login, but as **root** by default
  and without the user's GUI session — so it would need `UserName=` set to the
  owner and the same explicit `CR_DATA_DIR`/`CR_MODELS_DIR`/workspace pointing as
  §1.3.
- `KeepAlive` = the launchd analogue of systemd `Restart=always`; because the
  console is a foreground uvicorn process (it does not daemonize), it fits
  launchd's "must not daemonize" rule directly.
- `ProgramArguments` would today be `clear-record web --no-browser`; when the
  promised **`serve`** lands, that is the natural `ProgramArguments`.

**[DESIGN] Is the tray the desktop answer and `serve` the node answer?** Yes —
that is the coherent split, and it is already latent in the code:

- **Desktop:** `clear-record tray` starts and supervises the console in-process
  (ADR-0016); the user sees a menu-bar icon.
- **Node:** systemd (Linux) or `launchd` (macOS) supervises a headless
  `clear-record web --no-browser` **today**, and the intended `clear-record serve`
  once it is built. The tray must not be used on the node (no display; it would
  drag PySide6 into a daemon).

**[SUGGESTION]** The smallest credible node step is therefore **not** a new
container or Flatpak: it is (i) `launchd`/`systemd` around `clear-record web
--no-browser`, and (ii) the **missing `serve` subcommand** so the node runs a
command whose name matches its role (and which can carry `--supervise`). The
`ServiceController` seam already exists to make `serve --supervise` cheap.

---

## 8. What is **not** supported today (explicit)

- **A `serve` subcommand.** Not built; registered command names are `web`,
  `tray`, `mcp`. `web --no-browser` is the workaround.
- **Any authentication.** The console has none by design (ADR-0013); every route
  is open to whoever can reach the port.
- **A supported LAN/remote topology.** No auth, no TLS, no proxy template.
- **A queue.** Only "one active run per meeting"; no global FIFO, no queue
  position, no persisted scheduling.
- **Durable live-run events.** `RunState.events` is in memory; a restart 404s
  the run/event/UI endpoints and leaves the meeting showing a stale status.
- **Interrupted-run handling.** A restart leaves rows at `running`; there is no
  `interrupted` state and no reconciliation at startup.
- **A container image.** No `Dockerfile`/`Containerfile` or image is published by
  this project; the upstream `whisper.cpp` images are not clear-record images and
  do not contain the wheel.
- **A Flatpak background service.** Flatpak has no supported service model; the
  export request is an open issue.
- **A bundled model or vendor stack in any artifact.** Models are
  environment-local (ADR-0006); the vendor stack travels only as a documentation
  story or a build step, never in the wheel.
- **Credential-based secret loading.** The webhook secret is read from the
  process environment; `systemd` credentials are not consumed by the code.

---

## 9. Recommendation, ordered by cost

**[SUGGESTION]** Cheapest first; each is independently shippable.

1. **Document and support a plain systemd user unit + `loginctl enable-linger`,
   and a `launchd` LaunchAgent/LaunchDaemon for the Mac mini**, around today's
   `clear-record web --no-browser`, with `CR_DATA_DIR`/`CR_MODELS_DIR` set
   explicitly and the workspace left as a user path. *No code change; solves
   "run it as a service" on the node.*
2. **Build the missing `serve` subcommand** (`serve` = headless console with a
   `--supervise` option) so the node has a role-named command, reusing the
   Qt-free `ServiceController`. *Small, self-contained.*
3. **Close the state-durability gap**: persist the event stream (or a durable
   cursor), add an `interrupted` status and startup reconciliation, and derive
   the active-run guard from the registry. *Moderate; makes an always-on node
   trustworthy.*
4. **Add the minimal FIFO queue** (§6): one run at a time per node, persisted,
   with queue position in the UI. *Moderate; prevents a second run from
   oversubscribing one GPU.*
5. **Publish a container image** that builds `whisper.cpp` + a ggml backend
   (Vulkan first, CUDA via `--gpus`/CDI second), with volumes for
   workspace/models/XDG, a non-root `USER`, a `HEALTHCHECK` on `/api/health`,
   and a rootless Quadlet/Compose example. *Largest; only if the owner wants the
   container shape.*
6. **Then, and only then, decide §4** (localhost-only + tunnel/VPN, in-app auth,
   or proxy auth) — and write the ADR that records it. *The auth work is
   security-critical and should follow the shape decision, not precede it.*

---

## 10. Claims I could not source (all `[OPEN]`)

- **Whether uvicorn sends `sd_notify`** so a systemd `Type=notify` unit works
  (vs `Type=exec` + `ExecStartPost=` health poll).
- **Whether a `flatpak run` inside a headless systemd *user* unit starts cleanly**
  on a Linux box without a session/bus. The open Flatpak issue is evidence this is
  not a designed path, but I did not test it.
- **An actual container image size** for any GPU variant — not measured, not
  invented; only the dominant components and whisper.cpp's own model disk sizes
  are cited.
- **Docker's `--restart` policy wording** — I did not fetch that reference for
  this note (Podman Quadlet/systemd is cited instead).
- **An egress allow-list / firewalling story** for webhooks on a hardened node.
- **The exact app-owned data/cache/state split and the macOS/Windows XDG
  fallbacks** — still unresolved in ADR-0007.
- **Any news claim that a future Flatpak will gain `systemd-appd` app services** —
  secondary reporting only; not a Flatpak primary statement.

---

## Source list (primary owner first)

**Repository (the claim owner for its own behaviour)**
- `docs/architecture.md` §5 (hardware lab), §8 (resumable chunks, worker pool).
- `docs/adr/0013-bundled-web-and-service-surface.md` (localhost-only, no auth, `serve`).
- `docs/adr/0014-desktop-app-distribution.md` (never beyond localhost).
- `docs/adr/0015-flatpak-linux-distribution.md` (the sandbox wall, Vulkan choice).
- `docs/adr/0016-app-shell-htmx-tray-pi-agent.md` (tray entry point).
- `docs/adr/0007-deployment-directories-xdg.md` + Update 2026-09-13 (resolvers).
- `packages/clear-record/pyproject.toml`; `clear_record/web/__init__.py`;
  `clear_record/web/app.py`; `clear_record/tray/__init__.py`;
  `clear_record/tray/service.py`; `clear_record/service/runs.py`;
  `clear_record/service/paths.py`; `clear_record/service/webhooks.py`.

**systemd** (canonical man pages at freedesktop.org; the fetcher received HTTP 418
from `freedesktop.org`, so the text was read from systemd's own repository at
`main`, which is the owner of the man pages — retrieved 2026-09-15)
- `systemd.service(5)`:
  <https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.service.xml>
- `systemd.exec(5)`:
  <https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.exec.xml>
- `systemd-journald.service(8)`:
  <https://raw.githubusercontent.com/systemd/systemd/main/man/systemd-journald.service.xml>
- `user@.service(5)`:
  <https://raw.githubusercontent.com/systemd/systemd/main/man/user@.service.xml>
- `loginctl(1)`:
  <https://raw.githubusercontent.com/systemd/systemd/main/man/loginctl.xml>

**Containers**
- Docker, *Volumes*: <https://docs.docker.com/engine/storage/volumes/>
- Docker, *Dockerfile reference* (USER, HEALTHCHECK, VOLUME):
  <https://docs.docker.com/reference/dockerfile/>
- Docker, *GPU access*: <https://docs.docker.com/engine/containers/gpu/>
- Docker, `docker run --device`:
  <https://docs.docker.com/reference/cli/docker/container/run/#device>
- Podman, `podman-run(1)`:
  <https://docs.podman.io/en/latest/markdown/podman-run.1.html>
- Podman, `podman-systemd.unit(5)` (Quadlet):
  <https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html>
- NVIDIA, *Installing the NVIDIA Container Toolkit*:
  <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>
- ggml-org/whisper.cpp README (build flags, Docker images, model sizes):
  <https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/README.md>

**Flatpak**
- Flatpak, *Flatpak Command Reference* (v1.18.2; `flatpak-build-finish`,
  `flatpak-run`, no service command):
  <https://docs.flatpak.org/en/latest/flatpak-command-reference.html>
- Flatpak issue #2787, "Consider exporting systemd user units" (open, 2019-03-26):
  <https://github.com/flatpak/flatpak/issues/2787>

**Apple**
- Apple, *Daemons and Services Programming Guide → Creating Launch Daemons and
  Agents* (launchd, LaunchAgents vs LaunchDaemons, `KeepAlive`,
  `StandardOutPath`, do-not-daemonize):
  <https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html>

**Remote access / auth**
- OpenBSD, `ssh(1)` (`-L` local port forwarding): <https://man.openbsd.org/ssh.1>
- Tailscale, *tailscale serve*: <https://tailscale.com/kb/1242/tailscale-serve>
- nginx, `ngx_http_auth_basic_module`:
  <https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html>
- Caddy, `forward_auth`:
  <https://caddyserver.com/docs/caddyfile/directives/forward_auth>
