# ADR-0021 — Deployment is localhost-only; authentication is the operator's reverse proxy

Status: active
Date: 2026-09-15

Superseded **in part** by [ADR-0033](0033-the-auth-position.md) (2026-09-26): the
in-app auth position is decided, and **its build has landed** (the auth gate of
2026-09-26: one credential in the registry, human sessions, `/web/setup` and
`/health` the anonymous surface). This ADR's "ships no authentication" clause is
superseded by that decision and by that code; the ingress posture (the
operator's reverse proxy is the only ingress) stands, and its "the bind stays
localhost-only" reading is now **enforced**: a non-loopback bind with no named
trust source refuses to start.

## Context

- [VOICE: owner, 2026-09-15] Answering the service-deployment investigation,
  verbatim: *"do localhost only and let user to do the reverse proxy part"* —
  qualified **"(for now)"** in the owner's next message. Recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md). **This is a hedged
  decision**: option (b), in-app LAN authentication, is **deferred, not
  rejected**, and the "(for now)" is the owner's own framing.
- [FACT] The console binds `127.0.0.1` and ships **no authentication** — the
  loopback-only, no-auth posture is this ADR's own decision, and ADR-0013 ships
  the console and the `serve` command without stating either.
- [FACT] `docs/research/2026-09-15-clear-record-as-a-service.md` framed three
  options — (a) localhost + tunnel/VPN, (b) LAN bind + in-app auth, (c) LAN bind +
  reverse-proxy auth — with a cost order of **a < c < b**.
- [FACT] The console is **browser-accessible**, and its `/web/ui/*` endpoints accept
  **form-encoded POSTs**. A hostile page open in the same browser can therefore be
  induced to POST to `127.0.0.1` (CSRF), and DNS rebinding can make a remote name
  resolve to localhost. So "localhost-only" bounds **who can connect**, not **who
  can act** — a request guard is still required.
- [FACT] systemd (user unit), launchd and a container can each run
  `clear-record web --no-browser`; none of them implies a change to what we listen
  on.

## Decision

- [DECISION] The app **stays bound to `127.0.0.1` and ships no authentication**,
  **for now**. Option (b) — in-app LAN auth — is **deferred, not rejected**; the
  proxy is where auth lives today. *(Superseded 2026-09-26 by
  [ADR-0033](0033-the-auth-position.md): the console now carries one credential
  and its own sessions, and a bind past loopback needs a named trust source
  (`CR_TRUSTED_HOSTS`; a declared proxy peer is not one).
  The loopback bind stays the default and the proxy stays the ingress.)*
- [DECISION] **Remote access and authentication are the operator's reverse
  proxy** (nginx, Caddy, Tailscale, …). The app is the backend behind it and
  **never the ingress**.
- [DECISION] We therefore owe the operator two things: a **documented
  reverse-proxy recipe**, and a **guard that stops a hostile local page from
  acting on the console** — validating `Host` (DNS rebinding) and
  `Origin`/`Referer` (CSRF) on state-changing requests is the cheap, standard
  mitigation.
- [DECISION] Deployment shapes (systemd / launchd / container) are documented as
  **how to run the backend**, never as an auth story.
- [OPEN: owner, 2026-09-15, **closed 2026-09-26** — see the Update *the trusted
  proxies land*; quoted here as it stood while the item was open]
  **Honour `X-Forwarded-*` only from a configured trusted proxy
  (`CR_TRUSTED_PROXIES`) — deferred, not built.** The owner's steer
  was to accept forwarded headers from a declared proxy, with a **quick path for
  Tailscale**: `--tailscale` sets the proxy up itself, so it pre-trusts that hop
  instead of asking the operator to declare it. What is true today, scoped: the
  console's own code reads and honours **no** `X-Forwarded-*` header, and
  `CR_TRUSTED_PROXIES` is read as the **peer declaration** this item will honour —
  it is deliberately *not* what admits a non-loopback bind, because the auth
  gate's startup check reads `CR_TRUSTED_HOSTS` (the one declaration the guard's
  own `Host` check consults, and a proxy peer is not one). (The server under the
  console, uvicorn, does rewrite the scheme from a *loopback* peer's
  `X-Forwarded-Proto` by default, which is the shape this item narrows.) Honouring
  a declared peer's headers is this item's, so trusting forwarded headers from
  *anything else* remains a risk to the request guard; the bind is loopback by
  default, and the UI's relative URLs mean nothing is lost by
  refusing. Revisit when a proxy deployment
  genuinely needs the client's scheme or host. *(Direction 2026-09-26:
  [ADR-0033](0033-the-auth-position.md) puts trusted proxies in the auth build's
  scope; the code still reads neither header, so this item stays open-not-built
  until that lands.)*

## Rationale

- Preserves ADR-0013 and the local-first, single-user model.
- Authentication is security-critical; delegating it to a mature proxy beats a
  hand-rolled session + CSRF layer in a console whose audience is one person.
- Writing the operator's responsibility down stops "it's localhost-only, so it's
  safe" from becoming an unexamined assumption — the CSRF/DNS-rebinding guard is
  exactly the part that assumption would have left out.

## Discarded alternatives

- **In-app auth (option b)** — largest scope, security-critical, unnecessary for a
  single-user local tool; the proxy does it better.
- **Localhost alone, with no guard** — leaves CSRF and DNS rebinding open to a
  cheap fix; rejected.
- **Shipping our own TLS/proxy** — out of scope; the operator's stack already
  does this.

## Consequences / review hook

- The **reverse-proxy recipe** and the **request guard** become deliverables
  (ticket 02 in the local tracker's `service-deployment` lane).
- Operator documentation must say plainly: expose it only *through* the proxy, and
  the proxy must be the **only** ingress.
- Revisit if the product grows multi-user, or if it should ever be reachable
  without an operator-managed proxy.

## Update (2026-09-26) — the trusted proxies land

- [FACT] **Forwarded headers are honoured only from declared peers.** The request
  guard (`clear_record.web.guard`, installed in `clear_record.web.app`) resolves
  `X-Forwarded-Proto`, `X-Forwarded-Host` and `X-Forwarded-For` into the request
  **only** when the socket peer is a peer the operator declared
  (`CR_TRUSTED_PROXIES`, empty by default — so a stock install believes no
  forwarded header at all). A declared peer's `X-Forwarded-Proto` is what makes a
  proxied console's session cookie `Secure`: `web/auth.py`'s `secure_request`
  reads the request's scheme, and the guard writes that scheme before the auth
  gate reads anything — the guard's middleware runs outside the gate. Its
  `X-Forwarded-Host` is the authority the app's own URL building uses (port and
  all) — the console's links are relative today, so that is the foundation rather
  than a visible behaviour — and it is **not** the name the path-local rule reads:
  that rule takes the `Host` the client itself sent, so a forwarded `127.0.0.1`
  cannot make a remote client local. Its `X-Forwarded-For` is the address the
  request is attributed to, which nothing consumes yet (the server's access log
  prints the transport peer). A request from any other peer is judged by the
  socket it arrived on and the `Host` it carries: its forwarded headers are
  ignored rather than merged in, so a client cannot nominate its own scheme, name
  or address.
- [FACT] **The name a proxy forwards is still checked, and the bind rule is
  unchanged.** `CR_TRUSTED_PROXIES` is a **peer** declaration and not a trust
  source: a forwarded `Host` meets the same `Host` rule as a direct one, so the
  public hostname a proxy serves still belongs in `CR_TRUSTED_HOSTS` — the one
  declaration that admits a non-loopback bind, exactly as the startup refusal
  already said. Trusting a declared peer's headers says nothing about which names
  the console answers to.
- [FACT] **The server no longer decides it.** uvicorn's own `proxy_headers`
  handling believed a **loopback** peer's `X-Forwarded-Proto` whatever the
  operator declared, and honoured no `X-Forwarded-Host` at all — the shape this
  item narrowed. Every console posture now starts its server with that off
  (`NodeServer`), so the operator's declaration is the decision in one place
  instead of two.
- [FACT] **Tailscale keeps the quick path the steer asked for.** `--tailscale`
  declares the hop Serve proxies from — its target is always
  `http://127.0.0.1:<port>` — in-process, so a request over the tailnet still gets
  a `Secure` cookie with no second variable for the operator; an operator running
  `tailscale serve`, nginx or Caddy by hand declares that loopback peer
  themselves. The operator's half of this is written down in
  [`docs/service-deployment.md`](../service-deployment.md) §2, and §6 no longer
  lists it among what is not built.
