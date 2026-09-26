# Running clear-record as a service — behind your own reverse proxy

Status: operator guide
Date: 2026-09-15
Lane: the local tracker's `service-deployment` lane, ticket 02
(tracker lives in the main checkout).

Provenance: [FACT] claims are verifiable in this repo or in the sources the
research note cites; [DESIGN] is a chosen shape; [OPEN] is unresolved.

This guide is the operator half of [ADR-0021](adr/0021-localhost-only-deployment.md).
The reasoning — why localhost-only, why the proxy stays the ingress, what the
console's own credential adds to that (ADR-0033), and what was deferred — lives
in
[`docs/research/2026-09-15-clear-record-as-a-service.md`](research/2026-09-15-clear-record-as-a-service.md);
this page does not repeat it. Read that for the *why*; read this for the *how*.

## The shape, in one paragraph

[FACT, repo] `clear-record web` / `clear-record serve` start a uvicorn server that
**binds `127.0.0.1:8765` by default and gates everything it serves behind one
console password** (ADR-0013/ADR-0014; the credential itself is ADR-0033's —
[§1](#the-console-credential-adr-0033) below).
[DESIGN] It is a **backend**. Your reverse proxy is the **only ingress**: it
terminates TLS and forwards to `127.0.0.1:8765`. Nothing else should be able to
reach that port — a device that opens `http://<host>:8765/` directly reaches the
sign-in page rather than the console, but a password typed over plain HTTP on a
network is a password in the clear, so the port still belongs to loopback alone.

Say this out loud once, because it is the whole posture: **bound to localhost is
not the same as safe from the browser.** A hostile page open in the same browser
can POST to `127.0.0.1`, and DNS rebinding can make a remote name resolve to
localhost. That is why the console also carries a request guard (§2) — the part
a naive "it's localhost-only, so it's safe" story omits.

## 1. Run the backend

All three shapes run the same command:

```sh
clear-record serve --host 127.0.0.1 --port 8765
```

[FACT, repo] `serve` is the **node** entry point: it never opens a browser, it
writes the console's own logs to the **diagnostics sink** (the rotating file
`clear-record diagnose` reads, under the platform log directory or
`CR_LOG_DIR`), and it drives the run queue and startup reconciliation. Use it
for a systemd/launchd unit or a container. `clear-record web` is the
**interactive** entry point — the same console, but it opens a browser by
default and logs to the terminal — for a person at the machine. They are two
postures of one console, not two implementations. Keep the bind on `127.0.0.1`;
the proxy is what faces the network.

Set the data/model locations explicitly if you want them off the platform default
(the Linux default is the XDG data dir, ADR-0025):

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
ExecStart=%h/.local/bin/clear-record serve --host 127.0.0.1 --port 8765
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
`GET /health` — it and the setup page are what answer without a credential (the
compiled assets aside), and its whole body is `{"status": "ok"}`.

No service manager? `clear-record serve --supervise` is the stand-in: the node
keeps **itself** up — a server that stops without being asked is started again
over the same registry, after a short pause — while an asked-for stop
(`POST /api/shutdown`) or a signal ends it exactly as it does an unsupervised
node. No unit file and no root: just the command.

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
    <string>serve</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CR_DATA_DIR</key><string>/Users/you/Library/Application Support/clear-record</string>
    <key>CR_MODELS_DIR</key><string>/Users/you/Library/Application Support/clear-record/models</string>
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
  -e CR_TRUSTED_HOSTS=console.example.com \
  clear-record-web:local \
  clear-record serve --host 0.0.0.0 --port 8765
```

Two honest notes. `--host 0.0.0.0` is **inside** the container only — the
`--publish 127.0.0.1:8765:8765` keeps the exposed port loopback-only on the
host, and the request guard is what still refuses a rebound `Host` there;
`CR_TRUSTED_HOSTS` is required all the same, because a bind past loopback with
nothing declaring how the console is reached refuses to start (the paragraph
below). Add a
GPU with `--gpus all` (NVIDIA, via the Container Toolkit) or
`--device /dev/dri` (Vulkan), and a health probe against
`GET /health`; if the proxy is itself a container, put both on one network
and publish nothing at all.

### The console credential (ADR-0033)

[FACT, repo] The console gates everything it serves behind **one password**. A
fresh install has none: every route but `/setup`, `/health` and the compiled
assets redirects to `/setup` (the machine API answers `401` instead of a
redirect), and that page is where the first run sets the credential. The password
is stored in the registry as a salted `scrypt` hash — never in `config.toml`, in
the agent setup file, in a log line or in the diagnostics bundle — and signing in
holds a **server-side session**: the browser gets an opaque cookie (`HttpOnly`,
`SameSite=Lax`, scoped to the console, and `Secure` once TLS reaches the app), the
registry holds only that cookie's digest, and every request re-reads the row.

| Window | Default | What it means |
|---|---|---|
| Idle timeout | 12 hours | a session that sits unused this long is over; every accepted request moves the clock |
| Absolute lifetime | 30 days | a session never outlives this, however busy it is |

Sign out from the console's header. Settings → Status carries **Sign out
everywhere**, which ends every session this node has issued — both take effect on
the **next** request, with no restart.

**The rescue.** Lost the password, or the page will not let you in? On the machine
the node runs on:

```sh
clear-record password     # --data-dir / CR_DATA_DIR points at another registry
```

It prompts twice, writes the credential into the registry itself, prints the next
step, and needs no session, no browser and no running node. Replacing a credential
ends every session the old one opened, and a registry it cannot read is refused
rather than half-written.

**A bind past loopback needs a declared trust source.** A non-loopback `--host`
with nothing declared refuses to start, with a message naming the four ways
forward:

```
refusing to bind '0.0.0.0': the request guard answers 403 to any Host but
loopback, and nothing declares how this console is reached, so it would serve
nobody.
  Ways forward:
    - keep the loopback bind (the default) and let your reverse proxy be the ingress: drop --host
    - name the hostname this console answers to: CR_TRUSTED_HOSTS=<hostname>
    - declare the reverse proxy that fronts it: CR_TRUSTED_PROXIES=<peer address>
    - let Tailscale Serve front it: --tailscale
```

**The node's own machine.** A surface running as the same operating-system user
as the node — the command line first — is inside the boundary this gate defends
(ADR-0033), so it is never asked for the password: the node opens **one session
for its own machine** through the same path a sign-in uses and publishes its token
at `local-session` in its state directory (beside the address record), mode
`0600`, removed when the node exits cleanly. Anything that can read that file —
your own account, and root — can act on the console as you; nothing on the network
can, because the file is not served and a browser carries its own cookie. The node
keeps it current while it runs: a fresh one is minted when the old reaches a
deadline of its own clocks, or after **Sign out everywhere**, and a stale file is
refused exactly like any other dead session. Labelled machine tokens are the
scripting story of their own, and are not built yet.

The loopback default is unaffected: `clear-record serve` with no `--host` always
starts. `CR_TRUSTED_HOSTS` and `CR_TRUSTED_PROXIES` are the two declarations; §6
says exactly what is honoured from the second one today.

## 2. The request guard (on by default)

[FACT, repo] The guard runs **outside** the auth gate: a request with a `Host` the
console does not trust is refused `403` before a session is looked at, so DNS
rebinding and CSRF are answered whatever cookie a request carries.

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

[DESIGN] The name a request carries does a second job since ADR-0032's facade
landed: the node's **machine-facing** edge reads it to tell a client on this
machine from one that came in through your proxy. A request counts as local only
when it named the node **itself** — a loopback name, or the address the node is
listening on — and only such a client may name a **path** for the node to resolve
through the JSON API: a run's workspace directory, a meeting's `workspace_path`, a
tape's files, an archive root (one it hands over, or one it sets on a project).
A visitor arriving through `CR_TRUSTED_HOSTS` gets one sentence from the **JSON
API** and the shape to reach for instead, rather than having a path of its own —
or a same-named file on the node — acted on. Note what "the address the node is
listening on" costs: `Host` says which name was addressed and never where the
client sits, so if you bind the node to a named address and trust that name, a
client dialling it from the network is treated as local too — your choice of bind
and trust is what admits it, and the bind stays loopback-only by default for
exactly this reason. Two boundaries worth being clear about:

- **The console is not that edge.** The `/ui/*` pages and forms are the node's
  own in-process face (ADR-0032), so they are not guarded: a visitor who reaches
  the console through your proxy can still type a path in its forms, and that path
  is a folder **on the node**; the console shows back the path the node resolved.
  The refusal belongs to the JSON API, the surface where a program names what it
  wants.
- **Forward the original `Host`.** Caddy and Tailscale Serve do; nginx does with
  `$host` and does **not** with `$proxy_host`. Rewriting it to a loopback name
  would make every visitor look local, and a path typed on another machine would
  then be resolved here.

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
        proxy_set_header Host $host;   # the name the browser used — do not rewrite
    }
}
```

Create the password file with `htpasswd -c /etc/nginx/clear-record.htpasswd you`.
Be explicit that `Host` is passed through: **do not** rewrite it to
`127.0.0.1:8765`. The node reads the name the client addressed to tell a client on
its machine from one that came in through this proxy: a name that is the node's
own (its loopback, or the address it listens on) is what lets a client name a
**path** for the node to resolve through the JSON API — a run's workspace
directory, a tape, an archive root (ADR-0032) — while a client arriving through
this proxy is told, in one sentence, to address work the registry's way instead.
Rewriting `Host` would make every visitor look local, and a path typed on another
machine would then be resolved here. `CR_TRUSTED_HOSTS=console.example.com` is
required either way: `Host` is a name the guard must trust, and the browser's
`Origin` is the public name too. This is HTTP Basic auth: use TLS, and treat the
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

`clear-record serve --tailscale` (or `web --tailscale` interactively) does the
whole setup — resolve this machine's tailnet name, run Serve, trust that name in
the guard, and print the URL:

```sh
clear-record web --tailscale        # interactive: opens the console too
clear-record serve --tailscale      # for a service unit (no browser)
```

It reads the name from `tailscale status --json` (`Self.DNSName`, trailing dot
normalized), runs
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

> **The tailnet is the perimeter; the console has its own password.** The
> tailnet still decides *who can reach the console*, and since ADR-0033's gate
> landed the console also asks for its own credential — one password, its own
> sessions — so a device on your tailnet that is not yours still cannot read your
> meetings. Keep tailnet access controls and device approval as the outer layer.

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
`workspace_path` stay user documents — about **where the files live**. Since
[ADR-0032](adr/0032-the-node-and-its-clients.md) that is not the whole story
about a **run**: `clear-record run` starts a run the node owns and records, so a
run over a workspace writes a registry row and a client needs a node to talk to.

### The managed root

[FACT] The managed root defaults to `<data>/workspaces/` (for example
`~/.local/share/clear-record/workspaces` on Linux, or under your `CR_DATA_DIR`),
and is resolved by the same precedence as the other app-owned directories:
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

| Guard | Refuses | Status |
|---|---|---|
| filename sanitization | `..` traversal, absolute paths, separators, control characters | 400 |
| audio extension allow-list (`workspace.is_audio`) | a name that is not an audio type | 415 |
| size cap (`CR_MAX_UPLOAD_BYTES`, default 8 GiB) | a body over the cap — checked before the transfer, again while streaming | 413 |
| disk-space precheck | less free space than the declared body + headroom, checked **before** the body is read | 507 |
| no symlink following | a destination directory or file that resolves outside the managed root | 400 |
| upload-id format (`?upload_id=`) | an id that is not a bare `[A-Za-z0-9][A-Za-z0-9._-]*` token of at most 64 characters, checked **before** the body is read | 400 |
| upload-id reuse (`?upload_id=`) | an id whose scratch file is already on disk — an interrupted or in-flight transfer this node cannot resume | 501 |

An upload streams to a `.part` file beside its destination, `fsync`-es and
atomically renames it, then records the tape with its **sha256** and size. A
partial or dropped upload leaves **no** tape behind.

### Storage visibility and deleting tapes

[FACT] `GET /api/meetings/{id}/storage` reports the workspace size, the managed
root's **free space** (`free_bytes`, `null` when the meeting is not managed or
the filesystem cannot report it — the same accounting the upload guard checks),
and the uploaded tapes (path, sha256, size);
`DELETE /api/meetings/{id}/tapes/{tape_id}` deletes a **managed** tape. A tape in
a user-chosen workspace cannot be deleted here — it is your document.

> **Deleting requires a verified archive.** The route re-checks the meeting's
> archives against their manifests (`verify_archive`) and refuses with 400 when
> none verifies — *archive the meeting first* — unlinking nothing; the refusal
> names the archive action and the console's controls state the precondition.
> With a verified archive the delete proceeds, and the response's note names the
> archive that is the durable copy. An archive is a copy, never a move
> (ADR-0006); archive the
> meeting (`POST /api/meetings/{id}/archives`) before deleting its tapes.

Deleting a **glossary term** is likewise not a row delete: the console's and the
API's `DELETE` retire the term instead — the row survives with `added_by` and
`created_at`, stops biasing the decoder, and can be restored — `POST
/api/glossary/{id}/restore`, or the console's Restore button — to the status it
held before the retire (a retired candidate returns as a candidate, never as
owner-accepted truth).

### Security: uploads raise the stakes on the proxy

[DESIGN] Every surface before this one could only **read** local files. This one
**writes** multi-GB files to the node. ADR-0021's shape does not change — the
app still binds loopback, and the console's one credential is now in front of
every route but `/setup`, `/health` and the compiled assets — but the reason the
proxy must be
the **only** ingress is now sharper: anything that can *sign in* can reach an
upload endpoint and fill your disk. Keep the bind on `127.0.0.1`, keep the proxy in front, and
treat `CR_TRUSTED_HOSTS` as the minimal, explicit hatch it is (§2). The managed
root's permissions and free space are operator concerns; the guards bound an
upload, they do not make a public port safe.

### Limitation

[OPEN] Upload is a **single streaming POST**. A dropped multi-GB upload restarts
from zero. Chunked/resumable upload (tus or a resume token) is deliberately out
of scope until a real tape over a bad link makes it worth building.

[DESIGN] The endpoint does accept an optional **upload id** (`?upload_id=`),
validated and read before the body so a refusal costs no transfer. It names the
transfer's scratch file — the identity a resume layer would need — so
resumability can be added later without changing the request's shape. **Nothing
resumes today**: a fresh id still starts at zero, and an id whose scratch file is
already on disk is refused as unsupported (501) with the existing file left
untouched.

## 5. Run state, the queue, and restarts

[FACT, repo] The registry (SQLite) is the **source of truth** for runs, not the
console process's memory:

- **The event stream is persisted** per run as it arrives, so the live view
  replays after a restart instead of 404ing.
- **An orphaned run is reconciled**, at startup and by a queue whose next turn is
  blocked behind it. Every run records its owner as `host:pid`, so a run whose
  owner process is **gone** is an orphan at once: a killed console's run becomes
  **`interrupted`** (distinct from `failed`: the node died, the work did not
  necessarily fail) with the reason recorded on the run, its progress still
  readable, and you can start a new run. The meeting follows. When the owner is
  not a process this node can see — another host, or a run recorded before the
  owner column existed — the owner's refreshed **heartbeat** decides instead: a
  run with no beat at all is reaped at once, and one whose beat has gone stale
  is reaped after the deadline. A **stalled** owner (a process that
  still exists but has stopped reporting) keeps its run `running` and keeps
  holding the node and the meeting: the queue fails closed rather than admitting a
  second pipeline beside work that may still be progressing.
- **The active-run guard is read from the registry**, so a stale `running` row
  can no longer be silently doubled by a second start.

[FACT, repo] Every writer shares **one queue**. The console, the agent's MCP
server and the command line all enqueue into the same registry FIFO — `clear-record
run` is a client of the node (ADR-0032), so it starts its run there instead of
running the pipeline in its own process — and the move from `queued` to `running`
is **one conditional update**, so exactly one of them executes a run: a second
claimant loses cleanly and goes back to waiting, and a run the node is already
busy with is not claimable at all. Each run records the **origin** it was started
from — `console`, `api`, `mcp` or `cli` — so the row itself says where the work
came from.

[DESIGN] The node runs **one pipeline run at a time** — a persisted FIFO queue
in the registry. Starting a run while another is executing **enqueues** it and
reports its position (position 1 is next) instead of refusing the different
meeting, so two meetings no longer fight over one GPU. Queued work survives a
restart and is picked back up. This is deliberately minimal: no priorities, no
pausing a run (only cancel and resume), no per-meeting concurrency.

[FACT, repo] A run can be **cancelled** while it is queued or running, and a run
that stopped early can be **resumed** (RUN-04):

- **Queued**: cancelled outright. The row becomes `stopped` before anything claims
  it, so nothing runs it and a restart does not resurrect it.
- **Running**: *asked* to stop. Only the process executing a run may end it — its
  pipeline is mid-write in a workspace — so a cancel records a request on the row
  and the owner stops at its next safe boundary (a stage boundary, or between
  chunks in transcribe's pool, which also terminates the decoder children it
  launched) and writes `stopped` itself. A process that is stalled cannot read the
  request: its run stays `running`, which is why the console can say a run was
  *asked* to stop without claiming that it did.
- **Resume** starts a *new* run continuing the old one's work: it runs the
  previous run's own resolved options with `resume` on, so it re-uses the chunks
  the cache still holds, and the row records which run it resumes. The chunk
  cache is app-owned and keyed by the workspace path — a re-run from another
  workspace, another machine or a moved directory, decodes from scratch.

[FACT] `clear-record serve` stops draining the queue when it exits (a clean
`SIGTERM` included); a run still executing dies with the process, so the next
start reconciles it at once, and the resumable chunk cache means resuming it is
cheap (`docs/architecture.md` §8).

## 6. What this does not cover

- **Per-user accounts, RBAC, and machine tokens.** The console has **one**
  credential and no usernames (ADR-0033), and the machine API carries no bearer
  token yet — what exists is: the salted hash in the registry, the human sessions
  and their two windows, the `/setup` + `/health` anonymous surface, and the
  rescue command (§1). A token for scripts, per-project authorization, and an
  approvals ceremony are all deferred, not rejected; the rule they will meet is
  ADR-0033's — a credential alone may not destroy something no durable copy can
  reconstruct.
- **A published container image.** You build it, whisper.cpp and all.
- **Flatpak as a service.** [FACT] Flatpak has **no supported background-service
  model** — the request to export systemd user units is an open issue from 2019.
  Flatpak is the desktop bundle (ADR-0015), not the node.
- **Trusting `X-Forwarded-*`.** [OPEN] in ADR-0021 — deferred, not built: no
  code reads a forwarded header yet, so a proxied request is judged by the socket
  it arrived on and the session cookie follows *that* scheme. What `CR_TRUSTED_PROXIES`
  does today is the declaration half — naming the peer that fronts the console is
  one of the ways a non-loopback bind is allowed to start (§1) — while honouring
  the headers from exactly those peers is the trusted-proxy change's. The UI uses
  relative URLs, so a proxy that terminates TLS does not need them meanwhile.
- **Resumable/chunked upload.** A single POST restarts a dropped transfer
  (ADR-0024, §4).

## 7. Read more

- [ADR-0024](adr/0024-managed-workspace-tape-upload.md) — the managed workspace.
- [ADR-0021](adr/0021-localhost-only-deployment.md) — the decision.
- [ADR-0033](adr/0033-the-auth-position.md) — the credential, the sessions, and
  what the auth position does *not* build.
- [ADR-0013](adr/0013-bundled-web-and-service-surface.md) — localhost-only by
  default; its "no auth" note is superseded by ADR-0033.
- [Research: clear-record as a service](research/2026-09-15-clear-record-as-a-service.md)
  — systemd/container/Flatpak/launchd, the auth options, and state durability.
