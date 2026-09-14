"""Tailscale Serve integration for the console (ADR-0021).

Tailscale Serve is one of the reverse proxies ADR-0021 leaves remote access to:
it shares a **loopback** service inside an authenticated tailnet, terminates TLS
itself, and needs no in-app account. ``clear-record web --tailscale`` is the
one-command wrapper: it reads this machine's tailnet DNS name from
``tailscale status --json``, runs ``tailscale serve --bg http://127.0.0.1:<port>``,
and trusts that name in the request guard.

**The tailnet is the authentication.** The console keeps its loopback-only bind
and its no-auth posture; anyone who is a member of the tailnet can reach the
console, and there is no second password (ADR-0021).

The ``--bg`` flag is deliberately persistent: this module never tears the Serve
rule down on exit, and the operator removes it with
``tailscale serve --https=443 off`` (scoped to the mapping Serve created, not
``reset``, which clears every Serve rule on the machine).

Standard library only (``subprocess`` + ``json``); nothing here imports FastAPI,
so the light ``clear_record.web`` package can call it from the CLI. Every
failure raises :class:`TailscaleError` carrying an operator-facing fix, never a
traceback.
"""

from __future__ import annotations

import json
import subprocess
from urllib.parse import urlsplit

#: The Tailscale CLI; module-level so tests can point at a fake.
TAILSCALE_BIN = "tailscale"

#: Serve proxies **only** to loopback, so the target is fixed regardless of the
#: console's own ``--host`` (which stays localhost by default, ADR-0021).
SERVE_TARGET = "http://127.0.0.1:{port}"

#: Serve creates the default **HTTPS (port 443)** mapping. Tailscale's reference
#: requires the original protocol/port flags on an ``off`` command, and reserves
#: ``tailscale serve reset`` for clearing *all* Serve rules — so name the mapping
#: explicitly instead of the bare ``tailscale serve off``.
SERVE_OFF = "tailscale serve --https=443 off"

_INSTALL_HINT = (
    "install Tailscale, or make sure the `tailscale` command is on PATH "
    "(https://tailscale.com/download)"
)

_NOT_RUNNING_HINT = (
    "start Tailscale and sign in (`tailscale up`, or open the Tailscale app), "
    "then retry"
)


class TailscaleError(RuntimeError):
    """A Tailscale step failed. The message names the operator's fix."""


def normalize_name(value: str | None) -> str:
    """A hostname in the form the request guard compares.

    Lowercases, drops a trailing FQDN dot (``Self.DNSName`` ends in ``.``), and
    accepts a full URL by taking its host. Returns ``""`` for nothing usable.
    """
    if value is None:
        return ""
    text = value.strip()
    if text.startswith(("http://", "https://")):
        text = urlsplit(text).hostname or ""
    return text.lower().rstrip(".")


def _invoke(args: list[str], *, tailscale_bin: str = TAILSCALE_BIN):
    """Run the Tailscale CLI, mapping a missing binary to an actionable error."""
    try:
        return subprocess.run(
            [tailscale_bin, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TailscaleError(
            f"could not run `{tailscale_bin}`: {_INSTALL_HINT}"
        ) from exc
    except OSError as exc:
        raise TailscaleError(
            f"could not run `{tailscale_bin}` ({exc}): {_INSTALL_HINT}"
        ) from exc


def _detail(proc) -> str:
    """The CLI's own words for a failure — surfaced, never swallowed."""
    text = "\n".join(
        part.strip() for part in (proc.stderr or "", proc.stdout or "") if part.strip()
    )
    return text or f"exit status {proc.returncode}"


def resolve_dns_name(*, tailscale_bin: str = TAILSCALE_BIN) -> str:
    """This machine's tailnet DNS name, from ``tailscale status --json``.

    Prefers ``Self.DNSName`` (normalising its trailing dot); falls back to
    ``Self.HostName`` + ``CurrentTailnet.MagicDNSSuffix`` when the field is
    absent. Raises :class:`TailscaleError` when Tailscale is down, unauthenticated
    or reports nothing usable.
    """
    proc = _invoke(["status", "--json"], tailscale_bin=tailscale_bin)
    if proc.returncode != 0:
        raise TailscaleError(
            "could not read Tailscale status — is Tailscale logged in and "
            f"running?\n    `tailscale status --json` said: {_detail(proc)}\n"
            f"    Fix: {_NOT_RUNNING_HINT}."
        )
    try:
        status = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise TailscaleError(
            "`tailscale status --json` did not return JSON, so this machine's "
            f"tailnet name could not be read ({exc}).\n"
            "    Fix: check `tailscale version`; upgrade Tailscale if it is very old."
        ) from exc
    if not isinstance(status, dict):
        raise TailscaleError(
            "`tailscale status --json` returned an unexpected shape (not an "
            "object).\n    Fix: upgrade Tailscale, or pass "
            "--tailscale-host <machine>.<tailnet>.ts.net."
        )
    backend_state = status.get("BackendState")
    if isinstance(backend_state, str) and backend_state != "Running":
        raise TailscaleError(
            f"Tailscale reports its state as {backend_state!r}, so it is not up.\n"
            f"    Fix: {_NOT_RUNNING_HINT}."
        )
    self_node = status.get("Self")
    if not isinstance(self_node, dict):
        raise TailscaleError(
            "`tailscale status --json` had no 'Self' node, so this machine's "
            "tailnet name could not be read.\n"
            f"    Fix: {_NOT_RUNNING_HINT}, or pass "
            "--tailscale-host <machine>.<tailnet>.ts.net."
        )
    name = normalize_name(self_node.get("DNSName"))
    if not name:
        suffix = normalize_name(
            (status.get("CurrentTailnet") or {}).get("MagicDNSSuffix")
        )
        host = normalize_name(self_node.get("HostName"))
        if host and suffix:
            name = f"{host}.{suffix}"
    if not name:
        raise TailscaleError(
            "Tailscale reported no DNS name for this machine (MagicDNS may be "
            "off).\n    Fix: enable MagicDNS in the Tailscale admin console "
            "(DNS page), or pass --tailscale-host <machine>.<tailnet>.ts.net."
        )
    return name


def serve(port: int, *, tailscale_bin: str = TAILSCALE_BIN) -> str:
    """Run ``tailscale serve --bg http://127.0.0.1:<port>`` and return the target.

    ``--bg`` makes the rule outlive this process on purpose; remove it later with
    :data:`SERVE_OFF`. A refusal surfaces Tailscale's own stderr.
    """
    target = SERVE_TARGET.format(port=port)
    proc = _invoke(["serve", "--bg", target], tailscale_bin=tailscale_bin)
    if proc.returncode != 0:
        raise TailscaleError(
            f"`tailscale serve --bg {target}` was refused.\n"
            f"    Tailscale said: {_detail(proc)}\n"
            "    Fix: make sure HTTPS certificates are enabled for your tailnet "
            "(Tailscale admin console > DNS) and that you are logged in, then retry."
        )
    return target


def console_url(name: str) -> str:
    """The tailnet HTTPS URL Serve publishes for the console."""
    return f"https://{normalize_name(name)}/"


def disable_hint() -> str:
    """How the operator removes exactly the Serve mapping this flag created."""
    return SERVE_OFF


__all__ = [
    "SERVE_OFF",
    "SERVE_TARGET",
    "TAILSCALE_BIN",
    "TailscaleError",
    "console_url",
    "disable_hint",
    "normalize_name",
    "resolve_dns_name",
    "serve",
]
