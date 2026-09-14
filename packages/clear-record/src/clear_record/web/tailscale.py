"""Tailscale Serve integration for the console (ADR-0021).

Tailscale Serve is one of the reverse proxies ADR-0021 leaves remote access to:
it shares a **loopback** service inside an authenticated tailnet, terminates TLS
itself, and needs no in-app account. ``clear-record web --tailscale`` is the
one-command wrapper: it reads this machine's tailnet DNS name from
``tailscale status --json``, runs
``tailscale serve --https=<port> http://127.0.0.1:<console-port>``, and trusts
that name in the request guard.

**The tailnet is the authentication.** The console keeps its loopback-only bind
and its no-auth posture; anyone who is a member of the tailnet can reach the
console, and there is no second password (ADR-0021).

Lifecycle: Serve is run in its **foreground** form (no ``--bg``), as a real
child process, so the mapping lives exactly as long as the console does. A
foreground ``tailscale serve`` registers its rule under an ephemeral
``WatchIPNBus`` session, and Tailscale deletes that rule when the session
closes — which is what happens when the child exits, *including an ungraceful
exit* (``ipn.ServeConfig.Foreground`` exists precisely to keep a crash from
exposing a port nobody asked for). Terminating the child therefore **is** the
cleanup; this module never runs an ``off`` command, and a crash cannot leave a
stale rule behind.

Before touching anything, the current Serve config is snapshotted with
``tailscale serve status --json``. If the chosen port is already served — the
operator's own ``--bg`` rule, or another foreground session — that mapping is
theirs and is left untouched: the console still starts and trusts the tailnet
name, and says so. (tailscaled independently refuses a second listener on a
busy port, so this snapshot is a courtesy on top of that safety net.)

Standard library only (``subprocess`` + ``json`` + ``atexit``); nothing here
imports FastAPI, so the light ``clear_record.web`` package can call it from the
CLI. Every failure raises :class:`TailscaleError` carrying an operator-facing
fix, never a traceback.
"""

from __future__ import annotations

import atexit
import json
import subprocess
from urllib.parse import urlsplit

#: The Tailscale CLI; module-level so tests can point at a fake.
TAILSCALE_BIN = "tailscale"

#: Serve proxies **only** to loopback, so the target is fixed regardless of the
#: console's own ``--host`` (which stays localhost by default, ADR-0021).
SERVE_TARGET = "http://127.0.0.1:{port}"

#: Serve's protocol/port flag. Serve defaults to HTTPS on 443, but the console
#: always names the port so the mapping — and the printed URL — is unambiguous.
SERVE_HTTPS_FLAG = "--https={port}"

#: How long a freshly spawned foreground Serve may take to exit before we treat
#: it as up. Foreground Serve blocks for the console's whole lifetime, so a fast
#: exit means it was refused (bad flags, HTTPS off, daemon down, port in use).
SERVE_STARTUP_GRACE_S = 1.0

#: How long to wait for a terminated child to exit before escalating to SIGKILL.
#: Bounds how long cleanup can delay the console's own shutdown.
SERVE_STOP_GRACE_S = 2.0

_INSTALL_HINT = (
    "install Tailscale, or make sure the `tailscale` command is on PATH "
    "(https://tailscale.com/download)"
)

_NOT_RUNNING_HINT = (
    "start Tailscale and sign in (`tailscale up`, or open the Tailscale app), "
    "then retry"
)

_HTTPS_HINT = (
    "make sure HTTPS certificates are enabled for your tailnet "
    "(Tailscale admin console > DNS) and that you are logged in"
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
    """Run a one-shot Tailscale CLI command, mapping a missing binary to a fix."""
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


def _spawn(args: list[str]):
    """Start the long-lived foreground ``tailscale serve`` child.

    Its stdout/stderr are piped so a refusal can be surfaced; a successful
    foreground Serve is quiet after its banner, so the pipes cannot fill.
    """
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _detail(proc) -> str:
    """A completed CLI's own words for a failure — surfaced, never swallowed."""
    text = "\n".join(
        part.strip() for part in (proc.stderr or "", proc.stdout or "") if part.strip()
    )
    return text or f"exit status {proc.returncode}"


def _child_detail(process) -> str:
    """A finished child's own words, read from its (now drained) pipes."""
    parts: list[str] = []
    for stream in (process.stderr, process.stdout):
        if stream is None:
            continue
        try:
            text = stream.read()
        except (ValueError, OSError):
            continue
        if text and text.strip():
            parts.append(text.strip())
    return "\n".join(parts) or f"exit status {process.returncode}"


def _close_pipes(process) -> None:
    """Release a child's pipe handles (best effort; never raises)."""
    if process is None:
        return
    for stream in (process.stderr, process.stdout):
        if stream is None:
            continue
        try:
            stream.close()
        except (ValueError, OSError):
            pass


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


def serve_target(target_port: int) -> str:
    """The loopback target Serve proxies to (fixed host, chosen port)."""
    return SERVE_TARGET.format(port=target_port)


def serve_command(
    *, serve_port: int, target_port: int, tailscale_bin: str = TAILSCALE_BIN
) -> list[str]:
    """The **foreground** ``tailscale serve`` argv that publishes the console.

    ``--https=<serve_port>`` is the port the tailnet sees; the target is always
    ``http://127.0.0.1:<console-port>``, because Serve proxies only to loopback.
    """
    return [
        tailscale_bin,
        "serve",
        SERVE_HTTPS_FLAG.format(port=serve_port),
        serve_target(target_port),
    ]


def served_ports(config: object) -> frozenset[int]:
    """The tailnet ports a raw ``serve status --json`` config already serves.

    Covers the live (background) config and any ``Foreground`` session another
    ``tailscale serve`` left running. JSON object keys are strings: bare port
    numbers for ``TCP``, ``host:port`` for ``Web``. Tailscale **Services** are
    deliberately ignored — they answer on a separate virtual IP, not this node's
    port.
    """
    ports: set[int] = set()
    for section in _config_sections(config):
        tcp = section.get("TCP")
        if isinstance(tcp, dict):
            ports.update(port for key in tcp if (port := _as_port(key)) is not None)
        web = section.get("Web")
        if isinstance(web, dict):
            ports.update(
                port for key in web if (port := _port_from_hostport(key)) is not None
            )
    return frozenset(ports)


def read_served_ports(*, tailscale_bin: str = TAILSCALE_BIN) -> frozenset[int]:
    """Snapshot Serve's current ports, from ``tailscale serve status --json``.

    Returns an empty set when the config cannot be read (daemon down, an older
    CLI, unparseable output). The snapshot only protects a pre-existing rule,
    and tailscaled itself refuses a second listener on a busy port, so a failed
    read must not block the console from starting.
    """
    proc = _invoke(["serve", "status", "--json"], tailscale_bin=tailscale_bin)
    if proc.returncode != 0:
        return frozenset()
    try:
        config = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return frozenset()
    return served_ports(config)


def _config_sections(config: object):
    """The node config plus each foreground session it nests."""
    if not isinstance(config, dict):
        return
    yield config
    foreground = config.get("Foreground")
    if isinstance(foreground, dict):
        for session in foreground.values():
            if isinstance(session, dict):
                yield session


def _as_port(value: object) -> int | None:
    """A JSON object key as a port number, or None."""
    if isinstance(value, bool):  # bool is an int subclass; never a port
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _port_from_hostport(value: object) -> int | None:
    """The port out of a ``host:port`` web key (``[::1]:443`` included)."""
    if not isinstance(value, str):
        return None
    _, separator, tail = value.rpartition(":")
    if not separator:
        return None
    return _as_port(tail)


class ServeSession:
    """A console's Serve mapping: a live foreground child, or a reused rule.

    ``start_serve`` returns one of these. :meth:`stop` is idempotent and safe to
    call from a ``finally`` and again at interpreter exit.
    """

    def __init__(
        self,
        *,
        serve_port: int,
        target_port: int,
        process=None,
        reused: bool = False,
    ) -> None:
        self.serve_port = serve_port
        self.target_port = target_port
        self._process = process
        self.reused = reused

    @property
    def started(self) -> bool:
        """Whether this run created the mapping (False when it pre-existed)."""
        return self._process is not None

    def stop(self) -> None:
        """Terminate the child, which makes Tailscale drop the foreground rule.

        The child's ``WatchIPNBus`` session closes on exit, and tailscaled
        deletes the ephemeral mapping then — so there is no ``off`` command, and
        an ungraceful parent death cleans up too. Bounded: SIGTERM, a short
        wait, then SIGKILL; never blocks the console's shutdown for long.
        """
        process, self._process = self._process, None
        atexit.unregister(self.stop)
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=SERVE_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=SERVE_STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
        _close_pipes(process)


def start_serve(
    *, serve_port: int, target_port: int, tailscale_bin: str = TAILSCALE_BIN
) -> ServeSession:
    """Start foreground Serve for the console, or reuse a rule already on the port.

    If ``serve_port`` is already served, nothing is created: the pre-existing
    mapping is the operator's and is returned as ``reused``. Otherwise a real
    child is spawned; it blocks while serving, so a *fast* exit means it was
    refused and raises :class:`TailscaleError`. The caller turns that into a
    warning — the flag is a convenience, and the console must still start.
    """
    if serve_port in read_served_ports(tailscale_bin=tailscale_bin):
        return ServeSession(serve_port=serve_port, target_port=target_port, reused=True)
    args = serve_command(
        serve_port=serve_port, target_port=target_port, tailscale_bin=tailscale_bin
    )
    process = _spawn(args)
    try:
        process.wait(timeout=SERVE_STARTUP_GRACE_S)
    except subprocess.TimeoutExpired:
        return ServeSession(
            serve_port=serve_port, target_port=target_port, process=process
        )
    detail = _child_detail(process)
    _close_pipes(process)
    raise TailscaleError(
        f"`{' '.join(args)}` did not start.\n"
        f"    Tailscale said: {detail}\n"
        f"    Fix: {_HTTPS_HINT}, or pass --tailscale-port <port> to expose a "
        "different tailnet port."
    )


def console_url(name: str, port: int) -> str:
    """The tailnet HTTPS URL Serve publishes for the console.

    HTTPS on 443 is implicit in a URL; any other port is spelled out.
    """
    host = normalize_name(name)
    return f"https://{host}/" if port == 443 else f"https://{host}:{port}/"


__all__ = [
    "SERVE_HTTPS_FLAG",
    "SERVE_TARGET",
    "TAILSCALE_BIN",
    "ServeSession",
    "TailscaleError",
    "console_url",
    "normalize_name",
    "read_served_ports",
    "resolve_dns_name",
    "serve_command",
    "serve_target",
    "served_ports",
    "start_serve",
]
