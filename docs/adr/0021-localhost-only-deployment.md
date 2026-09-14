# ADR-0021 — Deployment is localhost-only; authentication is the operator's reverse proxy

Status: active
Date: 2026-09-15

## Context

- [VOICE: owner, 2026-09-15] Answering the service-deployment investigation,
  verbatim: *"do localhost only and let user to do the reverse proxy part"* —
  qualified **"(for now)"** in the owner's next message. Recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md). **This is a hedged
  decision**: option (b), in-app LAN authentication, is **deferred, not
  rejected**, and the "(for now)" is the owner's own framing.
- [FACT] ADR-0013 already binds the console to `127.0.0.1` with **no auth**.
- [FACT] `docs/research/2026-09-15-clear-record-as-a-service.md` framed three
  options — (a) localhost + tunnel/VPN, (b) LAN bind + in-app auth, (c) LAN bind +
  reverse-proxy auth — with a cost order of **a < c < b**.
- [FACT] The console is **browser-accessible**, and its `/ui/*` endpoints accept
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
  proxy is where auth lives today.
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
- [OPEN] Whether to honour `X-Forwarded-*` (`--proxy-headers`) when a proxy
  terminates TLS. The UI uses relative URLs, so it is probably unnecessary — and
  trusting forwarded headers from anything but the proxy is itself a risk, so
  verify before adding.

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
  (ticket 02 in `.scratch/service-deployment/`).
- Operator documentation must say plainly: expose it only *through* the proxy, and
  the proxy must be the **only** ingress.
- Revisit if the product grows multi-user, or if it should ever be reachable
  without an operator-managed proxy.
