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

The binary itself is located in one place, :func:`resolve_tailscale_bin`:
``CR_TAILSCALE`` wins over ``PATH`` (mirroring ``CR_WHISPER_CLI`` for the system
``whisper-cli``), and the located path is resolved through symlinks before it
reaches ``subprocess``. That resolution is load-bearing on macOS, where the App
Store install puts the CLI inside ``Tailscale.app`` and commonly symlinks
``~/.local/bin/tailscale`` at it: the app-bundle binary aborts when invoked
through the symlink, and works by its real path.

Standard library only (``json`` + ``os`` + ``shutil`` + ``atexit``); every child
process goes through the shared seam, :mod:`clear_record.core.process`. Nothing
here imports FastAPI, so the light ``clear_record.web`` package can call it from
the CLI. Every failure raises
:class:`TailscaleError` carrying an operator-facing fix, never a traceback.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from clear_record.core.process import CancellableProcessRunner, SubprocessRunner

#: The seam the one-shot CLI calls (``status --json``, ``serve status --json``)
#: go through. The long-lived Serve child gets a runner of its own, so stopping
#: the console's Serve cannot touch anything else.
_RUNNER = SubprocessRunner()

#: The Tailscale CLI; module-level so tests can point at a fake.
TAILSCALE_BIN = "tailscale"

#: Explicit binary override, mirroring ``CR_WHISPER_CLI`` for the system
#: ``whisper-cli``: a path here wins over ``PATH`` discovery. The documented
#: escape hatch for a non-standard install — the macOS app bundle above all.
TAILSCALE_BIN_ENV = "CR_TAILSCALE"

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
    "(https://tailscale.com/download); a non-standard install can be named "
    "explicitly with CR_TAILSCALE"
)

_NOT_RUNNING_HINT = (
    "start Tailscale and sign in (`tailscale up`, or open the Tailscale app), "
    "then retry"
)

_HTTPS_HINT = (
    "make sure HTTPS certificates are enabled for your tailnet "
    "(Tailscale admin console > DNS) and that you are logged in"
)

#: Markers that the CLI died before it could answer — a fatal runtime trap
#: (Swift ``fatalError``) or a crash — rather than a refusal that names its
#: reason. The macOS app-bundle ``bundleIdentifier`` abort lands here.
_ABORT_MARKERS = (
    "fatal error",
    "fatal:",
    "bundleidentifier",
    "unexpected fault address",
    "panic:",
    "traceback (most recent call last)",
    "segmentation fault",
    "illegal instruction",
    "abort trap",
    "sigabrt",
    "sigill",
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


def _resolve_path(path: str) -> str:
    """Follow ``path`` through symlinks to its real location, or leave it be."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def resolve_tailscale_bin() -> str:
    """The ``tailscale`` executable to invoke, with symlinks resolved in one place.

    Discovery order:

    1. ``CR_TAILSCALE`` — an explicit path wins over ``PATH``, the same escape
       hatch ``CR_WHISPER_CLI`` gives the system ``whisper-cli``.
    2. The first ``tailscale`` on ``PATH``.
    3. The bare name ``tailscale``, so a truly missing binary is still reported
       as the install hint rather than a resolution error.

    The located path is **resolved through symlinks** before it reaches
    ``subprocess``. On macOS the App Store install keeps the CLI inside
    ``Tailscale.app`` and commonly symlinks ``~/.local/bin/tailscale`` at it; the
    app-bundle binary **aborts** when ``argv[0]`` is that symlink because it
    cannot identify its own bundle (``BundleIdentifiers.swift``), while the same
    binary invoked by its real path works. Passing the resolved real path is what
    makes the default discovery work for those installs.
    """
    chosen = os.environ.get(TAILSCALE_BIN_ENV) or shutil.which(TAILSCALE_BIN)
    if not chosen:
        return TAILSCALE_BIN
    return _resolve_path(chosen)


def _invoke(args: list[str], *, tailscale_bin: str | None = None):
    """Run a one-shot Tailscale CLI command, mapping a missing binary to a fix.

    ``tailscale_bin`` is threaded by the callers that already resolved it (see
    :func:`resolve_tailscale_bin`); when omitted the binary is resolved here.
    """
    binary = tailscale_bin or resolve_tailscale_bin()
    try:
        return _RUNNER.run(
            [binary, *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TailscaleError(f"could not run `{binary}`: {_INSTALL_HINT}") from exc
    except OSError as exc:
        raise TailscaleError(
            f"could not run `{binary}` ({exc}): {_INSTALL_HINT}"
        ) from exc


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


def _looks_like_abort(proc) -> bool:
    """Whether the CLI **died** before answering, rather than refusing.

    A signal death (the negative return code ``subprocess`` reports for a trap
    or crash) or a fatal/crash signature in the output is an abort. A non-zero
    exit that printed **nothing at all** is one too: a refusal normally carries
    its reason on stderr, so silence is a crash. Anything else is Tailscale
    speaking for itself (e.g. the daemon-down message), which is a refusal.
    """
    if proc.returncode is not None and proc.returncode < 0:
        return True
    output = "\n".join((proc.stderr or "", proc.stdout or ""))
    if any(marker in output.lower() for marker in _ABORT_MARKERS):
        return True
    return not output.strip()


def _status_error(proc, tailscale_bin: str) -> TailscaleError:
    """The actionable error for a ``status --json`` that produced no status.

    An **abort** is named as an abort, never as a login problem — the original
    mis-diagnosis. The macOS App-Store case is called out with its two fixes.
    A **refusal** (Tailscale's own non-fatal message) keeps its words and the
    start-it fix, which is not claimed as a state we observed but as the next
    thing to try.
    """
    detail = _detail(proc)
    if _looks_like_abort(proc):
        return TailscaleError(
            "`tailscale status --json` died before it could report a status "
            "(a crash/abort).\n"
            f"    `{tailscale_bin} status --json` said: {detail}\n"
            "    On macOS this is what the App Store app bundle does when its "
            "CLI is invoked through a symlink — e.g. ~/.local/bin/tailscale -> "
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale: the "
            "app-bundle binary cannot identify its own bundle and aborts.\n"
            "    Fix: run the binary by its real path, or point the console at "
            "it with CR_TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/"
            "Tailscale."
        )
    return TailscaleError(
        "could not read Tailscale status.\n"
        f"    `{tailscale_bin} status --json` said: {detail}\n"
        f"    Fix: {_NOT_RUNNING_HINT}, or set CR_TAILSCALE if the CLI is "
        "installed outside PATH."
    )


def resolve_dns_name(*, tailscale_bin: str | None = None) -> str:
    """This machine's tailnet DNS name, from ``tailscale status --json``.

    Prefers ``Self.DNSName`` (normalising its trailing dot); falls back to
    ``Self.HostName`` + ``CurrentTailnet.MagicDNSSuffix`` when the field is
    absent. Raises :class:`TailscaleError` when Tailscale is down, unauthenticated
    or reports nothing usable.
    """
    binary = tailscale_bin or resolve_tailscale_bin()
    proc = _invoke(["status", "--json"], tailscale_bin=binary)
    if proc.returncode != 0:
        raise _status_error(proc, binary)
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
    *, serve_port: int, target_port: int, tailscale_bin: str | None = None
) -> list[str]:
    """The **foreground** ``tailscale serve`` argv that publishes the console.

    ``--https=<serve_port>`` is the port the tailnet sees; the target is always
    ``http://127.0.0.1:<console-port>``, because Serve proxies only to loopback.
    """
    binary = tailscale_bin or resolve_tailscale_bin()
    return [
        binary,
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


def read_served_ports(*, tailscale_bin: str | None = None) -> frozenset[int]:
    """Snapshot Serve's current ports, from ``tailscale serve status --json``.

    Returns an empty set when the config cannot be read (daemon down, an older
    CLI, unparseable output). The snapshot only protects a pre-existing rule,
    and tailscaled itself refuses a second listener on a busy port, so a failed
    read must not block the console from starting.
    """
    binary = tailscale_bin or resolve_tailscale_bin()
    proc = _invoke(["serve", "status", "--json"], tailscale_bin=binary)
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
        runner: CancellableProcessRunner | None = None,
        reused: bool = False,
    ) -> None:
        self.serve_port = serve_port
        self.target_port = target_port
        self._process = process
        self._runner = runner
        self.reused = reused

    @property
    def started(self) -> bool:
        """Whether this run created the mapping (False when it pre-existed)."""
        return self._process is not None

    def stop(self) -> None:
        """Terminate the child, which makes Tailscale drop the foreground rule.

        The child's ``WatchIPNBus`` session closes on exit, and tailscaled
        deletes the ephemeral mapping then — so there is no ``off`` command, and
        an ungraceful parent death cleans up too. The ladder itself is the
        seam's (:meth:`clear_record.core.process.CancellableProcessRunner.stop`):
        SIGTERM, ``SERVE_STOP_GRACE_S`` seconds, then SIGKILL — never blocks the
        console's shutdown for long, and only this session's child is signalled.
        """
        process, self._process = self._process, None
        runner, self._runner = self._runner, None
        atexit.unregister(self.stop)
        if process is None:
            return
        if runner is None:
            # Built by hand (a test, a caller using the constructor directly):
            # no runner owns the child, so give the ladder a private one. It
            # signals this child and nothing else either way.
            runner = CancellableProcessRunner()
        runner.stop(process, grace=SERVE_STOP_GRACE_S)
        _close_pipes(process)


def start_serve(
    *, serve_port: int, target_port: int, tailscale_bin: str | None = None
) -> ServeSession:
    """Start foreground Serve for the console, or reuse a rule already on the port.

    If ``serve_port`` is already served, nothing is created: the pre-existing
    mapping is the operator's and is returned as ``reused``. Otherwise a real
    child is spawned; it blocks while serving, so a *fast* exit means it was
    refused and raises :class:`TailscaleError`. The caller turns that into a
    warning — the flag is a convenience, and the console must still start.
    """
    binary = tailscale_bin or resolve_tailscale_bin()
    if serve_port in read_served_ports(tailscale_bin=binary):
        return ServeSession(serve_port=serve_port, target_port=target_port, reused=True)
    args = serve_command(
        serve_port=serve_port, target_port=target_port, tailscale_bin=binary
    )
    # A runner of this session's own: it tracks the foreground Serve child, so
    # stopping the console signals exactly that child and nothing else. Its
    # stdout/stderr are piped so a refusal can be surfaced; a successful
    # foreground Serve is quiet after its banner, so the pipes cannot fill.
    runner = CancellableProcessRunner()
    process = runner.start(args, capture_output=True, text=True)
    try:
        process.wait(timeout=SERVE_STARTUP_GRACE_S)
    except subprocess.TimeoutExpired:
        return ServeSession(
            serve_port=serve_port,
            target_port=target_port,
            process=process,
            runner=runner,
        )
    detail = _child_detail(process)
    runner.stop(process, grace=SERVE_STOP_GRACE_S)
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
    "TAILSCALE_BIN_ENV",
    "ServeSession",
    "TailscaleError",
    "console_url",
    "normalize_name",
    "read_served_ports",
    "resolve_dns_name",
    "resolve_tailscale_bin",
    "serve_command",
    "serve_target",
    "served_ports",
    "start_serve",
]
