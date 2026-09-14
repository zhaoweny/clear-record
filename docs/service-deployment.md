# Running clear-record as a service — behind your own reverse proxy

Status: operator guide
Date: 2026-09-15
Lane: `.scratch/service-deployment`, ticket
[`02-proxy-recipe-and-guard.md`](../.scratch/service-deployment/issues/02-proxy-recipe-and-guard.md)
(tracker lives in the main checkout).

Provenance: [FACT] claims are verifiable in this repo or in the sources the
research note cites; [DESIGN] is a chosen shape; [OPEN] is unresolved.

This guide is the operator half of [ADR-0021](adr/0021-localhost-only-deployment.md).
The reasoning — why localhost-only, why the proxy owns authentication, what was
deferred — lives in
[`docs/research/2026-09-15-clear-record-as-a-service.md`](research/2026-09-15-clear-record-as-a-service.md);
this page does not repeat it. Read that for the *why*; read this for the *how*.

## The shape, in one paragraph

[FACT, repo] `clear-record web` starts a uvicorn server that **binds
`127.0.0.1:8765` by default and ships no authentication** (ADR-0013/ADR-0014).
[DESIGN] It is a **backend**. Your reverse proxy is the **only ingress**: it
terminates TLS, authenticates you, and forwards to `127.0.0.1:8765`. Nothing
else should be able to reach that port — if another device can open
`http://<host>:8765/` directly, it bypasses every control on this page.

Say this out loud once, because it is the whole posture: **bound to localhost is
not the same as safe from the browser.** A hostile page open in the same browser
can POST to `127.0.0.1`, and DNS rebinding can make a remote name resolve to
localhost. That is why the console also carries a request guard (§2) — the part
a naive "it's localhost-only, so it's safe" story omits.

## 1. Run the backend

All three shapes run the same command:

```sh
clear-record web --no-browser --host 127.0.0.1 --port 8765
```

[FACT, repo] There is **no `serve` subcommand** — `web` is the registered
command, and `--no-browser` keeps a daemon from trying to open a desktop
browser. Keep the bind on `127.0.0.1`; the proxy is what faces the network.

Set the data/model locations explicitly so the service does not depend on its
working directory (ADR-0007):

```sh
Environment=CR_DATA_DIR=%h/.local/share/clear-record
Environment=CR_MODELS_DIR=%h/.local/share/clear-record/models
```

A workspace is normally a **user document**: your recordings and the derived
record, left where they are and pointed at by a meeting. Do not move such a
workspace under the app's data directory. The one exception is a **managed
workspace** (ADR-0024, §4), which the console creates under data so you can
**upload** tapes to the node instead of placing them by hand.

### systemd (Linux, user unit)

[FACT] A **user unit** matches the app's per-user model and needs no root; on a
headless always-on node, `loginctl enable-linger "$USER"` keeps the user
manager (and the unit) alive after logout. Save as
`~/.config/systemd/user/clear-record.service`:

```ini
[Unit]
Description=clear-record console (headless)

[Service]
Type=exec
ExecStart=%h/.local/bin/clear-record web --no-browser --host 127.0.0.1 --port 8765
WorkingDirectory=%h
Environment=CR_DATA_DIR=%h/.local/share/clear-record
Environment=CR_MODELS_DIR=%h/.local/share/clear-record/models
# Add the hostname your proxy serves, so the guard trusts the browser's Origin:
# Environment=CR_TRUSTED_HOSTS=console.example.com
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
systemctl --user enable --now clear-record.service
loginctl enable-linger "$USER"   # only if it must outlive your login
```

`Type=exec` reports a start failure if the binary cannot be invoked;
`Restart=on-failure` deliberately does not resurrect a deliberate stop. If
webhooks are configured, their signing secret is read from the process
environment at delivery time — put it in an `EnvironmentFile=` with mode
`0600` and accept that systemd warns environment variables are not a secret
store (research note §1.4). Readiness can be polled from `ExecStartPost=` at
`GET /api/health`.

### macOS (launchd agent)

[FACT] Launch**Agents** run in the user's GUI session and stop on logout; a
Mac mini that must serve headless therefore needs auto-login, or a
Launch**Daemon** in `/Library/LaunchDaemons` with `UserName=` set to your
account. Save as `~/Library/LaunchAgents/com.clear-record.web.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.clear-record.web</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.local/bin/clear-record</string>
    <string>web</string>
    <string>--no-browser</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CR_DATA_DIR</key><string>/Users/you/.local/share/clear-record</string>
    <key>CR_MODELS_DIR</key><string>/Users/you/.local/share/clear-record/models</string>
    <!-- <key>CR_TRUSTED_HOSTS</key><string>console.example.com</string> -->
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/you/Library/Logs/clear-record.log</string>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/clear-record.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clear-record.web.plist
launchctl print gui/$(id -u)/com.clear-record.web
```

[FACT] Processes managed by launchd must not daemonize; the console is a
foreground uvicorn process, so it fits. `KeepAlive` is the launchd analogue of
systemd's restart policy.

### Container

[FACT, repo] **This project publishes no container image**, and a working image
is ours to build: the base has no `whisper-cli` and no ggml backend, so the
image must bundle one (research note §2, "the same wall ADR-0015 hit"). Never
bake model weights into the image (ADR-0006).

Once you have built such an image, run it so that **only the host's loopback**
is published, and let the host's proxy face the network:

```sh
docker run --detach --name clear-record \
  --publish 127.0.0.1:8765:8765 \
  --volume clear-record-data:/data \
  --volume clear-record-models:/models \
  --volume /path/to/workspace:/workspace \
  -e CR_DATA_DIR=/data -e CR_MODELS_DIR=/models \
  clear-record-web:local \
  clear-record web --no-browser --host 0.0.0.0 --port 8765
```

Two honest notes. `--host 0.0.0.0` is **inside** the container only — the
`--publish 127.0.0.1:8765:8765` keeps the exposed port loopback-only on the
host, and the request guard is what still refuses a rebound `Host` there. Add a
GPU with `--gpus all` (NVIDIA, via the Container Toolkit) or
`--device /dev/dri` (Vulkan), and a health probe against
`GET /api/health`; if the proxy is itself a container, put both on one network
and publish nothing at all.

## 2. The request guard (on by default)

[FACT, repo] `clear_record.web.guard`, installed as middleware in
`clear_record.web.app`, rejects a hostile browser's requests before any handler
runs and answers `403` with an actionable `detail`:

| Check | Applies to | Rejects | Why |
|---|---|---|---|
| `Host` | **every** request | a host that is neither loopback nor named in `CR_TRUSTED_HOSTS` | DNS rebinding; a rebound `GET` is still a disclosure |
| `Origin` / `Referer` | **state-changing** requests (`POST`/`PUT`/`PATCH`/`DELETE`) | an origin that is neither loopback nor trusted | CSRF from a hostile page |

A state-changing request with **neither** header (a script, `curl`, the MCP
client) is allowed: browsers attach `Origin` to cross-origin state-changing
requests, so its absence is not the attack shape. Ordinary same-origin `GET`s
and `POST`s are untouched.

Trusted hosts are `127.0.0.1`, `localhost` (and `*.localhost`) and `::1`, plus
anything in the comma-separated **`CR_TRUSTED_HOSTS`** environment variable:

```sh
Environment=CR_TRUSTED_HOSTS=console.example.com,myhost.tailnet.ts.net
```

[DESIGN] `CR_TRUSTED_HOSTS` is the deliberate escape hatch for a proxy setup,
because the browser's `Origin` is the **public** hostname even though the
request arrives over loopback. Naming hosts in that variable was chosen over a
global "disable the guard" switch: the hatch stays explicit and minimal, and
loopback keeps working regardless. Set it to every hostname you serve the
console on; a stock install leaves it unset and trusts loopback only.

## 3. Front it with a proxy

### nginx (`auth_basic`)

[FACT] `auth_basic` / `auth_basic_user_file` validate a user name and password.

```nginx
server {
    listen 443 ssl;
    server_name console.example.com;

    location / {
        auth_basic "clear-record";
        auth_basic_user_file /etc/nginx/clear-record.htpasswd;

        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host 127.0.0.1:8765;   # the guard's loopback default
    }
}
```

Create the password file with `htpasswd -c /etc/nginx/clear-record.htpasswd you`.
nginx's default upstream `Host` (`$proxy_host`) is already `127.0.0.1:8765`, but
being explicit documents the intent. You still need
`CR_TRUSTED_HOSTS=console.example.com` on the service, because the browser's
`Origin` is the public name. This is HTTP Basic auth: use TLS, and treat the
password as the only barrier between the internet and a console with no
accounts of its own.

### Caddy (`forward_auth`)

[FACT] `forward_auth` proxies an authentication check to an external gateway
and continues only on a `2xx` — pair it with an identity gateway such as
Authelia. For a single user, Caddy's built-in `basic_auth` is the smaller
option.

```caddyfile
console.example.com {
    forward_auth 127.0.0.1:9091 {
        uri /api/verify?rd=https://auth.example.com/
        copy_headers Remote-User Remote-Groups
    }
    reverse_proxy 127.0.0.1:8765
}
```

Caddy passes the original `Host` through, so set
`CR_TRUSTED_HOSTS=console.example.com` and the guard will accept both the
`Host` and the browser's `Origin`. Caddy obtains and renews TLS automatically.

### Tailscale

[FACT] Tailscale Serve shares a local service inside the tailnet and proxies
only to a loopback target; the tailnet is itself authenticated by Tailscale, so
there is no separate password.

`clear-record web --tailscale` does the whole setup — resolve this machine's
tailnet name, run Serve, trust that name in the guard, and print the URL:

```sh
clear-record web --tailscale                 # interactive: opens the console too
clear-record web --tailscale --no-browser    # for a service unit
```

It reads the name from `tailscale status --json` (`Self.DNSName`, trailing dot
normalised), runs
`tailscale serve --https=<port> http://127.0.0.1:<console-port>`, and prints the
URL (`https://<machine>.<tailnet>.ts.net/`, or with `:<port>` when the exposed
port is not 443).

#### Finding the `tailscale` binary

[DESIGN] The CLI is located once, in this order: **`CR_TAILSCALE`** (an explicit
path — the same escape hatch `CR_WHISPER_CLI` gives the system `whisper-cli`),
then the first `tailscale` on `PATH`. The located path is then **resolved
through symlinks** before it is run. That matters on macOS: the App Store install
keeps the CLI inside `Tailscale.app` and usually symlinks
`~/.local/bin/tailscale -> /Applications/Tailscale.app/Contents/MacOS/Tailscale`,
and the app-bundle binary **aborts** when it is invoked *through* the symlink
(it cannot identify its own bundle, `BundleIdentifiers.swift:47`), while the same
binary works by its real path. The resolution is automatic; point at a
non-standard install explicitly with `CR_TAILSCALE`:

```sh
CR_TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale \
  clear-record web --tailscale
```

[FACT] The Serve invocation is the **1.52+ CLI form**. Verified against the
[Tailscale Serve command
reference](https://tailscale.com/kb/1242/tailscale-serve): a port / partial URL
/ full URL is a valid `<target>`; a reverse proxy accepts **only**
`http://127.0.0.1`; the exposed HTTPS port is set with `--https=<port>` (443 is
the flag's default); and `--bg` is what makes a mapping survive the invoking
process. Older clients predate that form; upgrade Tailscale if the command is
refused.

#### The exposed port

[DESIGN] Both ports default to the same number (8765), so a plain `--tailscale`
publishes `https://<machine>.<tailnet>.ts.net:8765/`. Choose a different tailnet
port with `--tailscale-port` (the console's own `--port` is unchanged):

```sh
clear-record web --tailscale --tailscale-port 443   # https://<machine>.<tailnet>.ts.net/
```

The Serve **target** is always `http://127.0.0.1:<--port>`: Serve proxies only
to loopback, and `--host` must therefore stay loopback (`127.0.0.1`, `::1`,
`localhost`). `--tailscale --host 0.0.0.0` is refused with a usage error rather
than publishing a target that would 502 to the whole tailnet.

#### The mapping lives and dies with the console

[DESIGN] `--tailscale` runs Serve in its **foreground** form (no `--bg`), as a
real child of the console. A foreground Serve serves until it is interrupted and
registers its rule under an ephemeral `WatchIPNBus` session; Tailscale deletes
that rule when the session closes — which is what happens when the process
exits, graceful or not. So terminating the child **is** the cleanup: Ctrl-C
(`SIGINT`), `SIGTERM` and a normal stop all take the mapping with them, and a
crash cannot leave a stale rule behind (that ephemerality is exactly what
`ipn.ServeConfig.Foreground` is for). No `tailscale serve off` is needed, and
none is run.

#### A rule that was already there is left alone

Snapshotting first is what makes this safe. Before changing anything,
`--tailscale` reads `tailscale serve status --json`. If the chosen tailnet port
is already served — your own `--bg` mapping, or another foreground session — the
flag creates nothing, leaves that mapping untouched, and says so. (tailscaled
independently refuses a second listener on a busy port, so the snapshot is a
courtesy on top of that safety net.) If the existing rule is not the console,
pick a free port with `--tailscale-port`.

> **The tailnet is the authentication.** The console still ships no accounts of
> its own, so **anyone who can reach your tailnet can reach the console.** If
> that is not what you want, use tailnet access controls and device approval —
> there is no second password on this path (ADR-0021).

[DESIGN] A **refused Serve is a warning, not a dead console**: the flag is a
convenience, so if Serve cannot start the console starts anyway with the tailnet
name trusted, and the message names the fix. A Tailscale problem never takes the
local console down with it.

If the machine's name is not the one to trust (a renamed or unusual tailnet),
override the resolved name — `tailscale status` is then not consulted:

```sh
clear-record web --tailscale --tailscale-host machine.tailnet.ts.net
```

`CR_TRUSTED_HOSTS` still composes: the tailnet name is **added** to whatever
that variable names, so an existing public hostname keeps working.

Failures are actionable messages, never tracebacks: `tailscale` not on `PATH`
(name a non-standard install with `CR_TAILSCALE`); the daemon down or logged out
(`tailscale up`); a refused `serve` (its own `stderr` is surfaced); no DNS name
reported (MagicDNS off — pass `--tailscale-host`); or unexpected `status --json`
output. A CLI that **dies before it answers** — a fatal/abort signature, a signal
death, or a non-zero exit with no output — is reported as an *abort*, with the
macOS app-bundle/symlink cause above and the `CR_TAILSCALE` fix, and never as "not
logged in": an abort is not a login state. Each names the fix.

Open the printed address. This is the lowest-effort remote shape: no public
port, no proxy auth config.

## 4. Managed workspaces and tape upload

[DESIGN] A **managed workspace** is app-owned *additionally*: when you create a
meeting with `managed: true` (the console's managed mode), clear-record creates
its workspace under the managed root, and you **upload** the tapes to the node
instead of placing them by hand first. The pipeline then runs over that
workspace exactly as it would over a `--dir` one — an uploaded tape is added to
the meeting's tape set like a path is, so runs, archiving and the MCP surface
need no new plumbing ([ADR-0024](adr/0024-managed-workspace-tape-upload.md)).
ADR-0007 is amended only here: a `--dir` workspace and a meeting's user-chosen
`workspace_path` stay user documents.

### The managed root

[FACT] The managed root defaults to `<data>/workspaces/` (so
`$XDG_DATA_HOME/clear-record/workspaces`, or under your `CR_DATA_DIR`), and is
resolved by the same precedence as the other app-owned directories:
explicit argument > **`CR_WORKSPACE_ROOT`** > a `[paths] workspace_root` config
entry > the data-dir default. The tapes are the largest thing the app stores,
so point the root at a NAS or a dedicated disk:

```sh
Environment=CR_WORKSPACE_ROOT=/mnt/tapes/clear-record
```

Then back up (or snapshot) that root. It holds the **only** copy of an uploaded
tape until you archive the meeting.

### Upload guards

[DESIGN] Upload writes files, so it is guarded by construction. Each refusal is
an actionable message, not a traceback:

| Guard | Refuses | Raise |
|---|---|---|
| filename sanitisation | `..` traversal, absolute paths, separators, control characters | 400 |
| audio extension allow-list (`workspace.is_audio`) | a name that is not an audio type | 415 |
| size cap (`CR_MAX_UPLOAD_BYTES`, default 8 GiB) | a body over the cap — checked before the transfer, again while streaming | 413 |
| disk-space precheck | less free space than the declared body + headroom, checked **before** the body is read | 507 |
| no symlink following | a destination directory or file that resolves outside the managed root | 400 |

An upload streams to a `.part` file beside its destination, `fsync`-es and
atomically renames it, then records the tape with its **sha256** and size. A
partial or dropped upload leaves **no** tape behind.

### Storage visibility and deleting tapes

[FACT] `GET /api/meetings/{id}/storage` reports the workspace size and the
uploaded tapes (path, sha256, size); `DELETE /api/meetings/{id}/tapes/{tape_id}`
deletes a **managed** tape. A tape in a user-chosen workspace cannot be deleted
here — it is your document.

> **The archive is the durable copy.** Deleting workspace tapes frees the node;
> archive the meeting first (`POST /api/meetings/{id}/archives`) if you need to
> keep it. An archive is a copy, never a move (ADR-0006).

### Security: uploads raise the stakes on the proxy

[DESIGN] Every surface before this one could only **read** local files. This one
**writes** multi-GB files to the node. ADR-0021's shape does not change — the
app still binds loopback and ships no auth — but the reason the proxy must be
the **only** ingress is now sharper: anything that can reach an upload endpoint
can fill your disk. Keep the bind on `127.0.0.1`, keep the proxy in front, and
treat `CR_TRUSTED_HOSTS` as the minimal, explicit hatch it is (§2). The managed
root's permissions and free space are operator concerns; the guards bound an
upload, they do not make a public port safe.

### Limitation

[OPEN] Upload is a **single streaming POST**. A dropped multi-GB upload restarts
from zero. Chunked/resumable upload (tus or a resume token) is deliberately out
of scope until a real tape over a bad link makes it worth building.

## 5. What this does not cover

- **In-app authentication.** There is none, by design, for now (ADR-0021
  defers, not rejects, LAN auth). The proxy is the auth.
- **A `serve` subcommand.** Not built; use `web --no-browser`.
- **A published container image.** You build it, whisper.cpp and all.
- **Flatpak as a service.** [FACT] Flatpak has **no supported background-service
  model** — the request to export systemd user units is an open issue from 2019.
  Flatpak is the desktop bundle (ADR-0015), not the node.
- **Trusting `X-Forwarded-*`.** [OPEN] in ADR-0021; the UI uses relative URLs,
  so a proxy that terminates TLS does not need them today.
- **Resumable/chunked upload.** A single POST restarts a dropped transfer
  (ADR-0024, §4).
- **The upload UI.** The upload endpoint, storage report and delete endpoint are
  built; the console's upload control is a follow-up slice.

## 6. Read more

- [ADR-0024](adr/0024-managed-workspace-tape-upload.md) — the managed workspace.
- [ADR-0021](adr/0021-localhost-only-deployment.md) — the decision.
- [ADR-0013](adr/0013-bundled-web-and-service-surface.md) — localhost-only, no auth.
- [Research: clear-record as a service](research/2026-09-15-clear-record-as-a-service.md)
  — systemd/container/Flatpak/launchd, the auth options, and state durability.
