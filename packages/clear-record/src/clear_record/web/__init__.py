"""The bundled web surface for clear-record.

Importing this package is **deliberately light**: it must not import FastAPI or
uvicorn, because the CLI discovers the ``web`` subcommand through an entry point
while building the parser — and a plain `clear-record ingest` should not pay the
web stack's import cost. The app is imported inside the handler.

The subcommand is registered through the ``clear_record.commands`` entry-point
group (ADR-0013) so ``clear_record.cli`` never statically imports this module.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import signal
import threading
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from clear_record.web.tailscale import ServeSession

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Everything the console needs at run time. `clear_record.web` itself is always
#: importable (it ships in the one wheel); these are the optional `web` extra.
_WEB_STACK = ("fastapi", "uvicorn", "jinja2", "python_multipart")

_MISSING_EXTRA_HINT = (
    "[web] the web console needs the optional 'web' extra ({missing} not found).\n"
    "  Install it with:   pip install 'clear-record[web]'\n"
    "  Or run without installing:   uvx --from 'clear-record[web]' clear-record web"
)


def _require_web_stack() -> None:
    """Fail with an actionable hint when the `web` extra is not installed."""
    missing = [name for name in _WEB_STACK if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit(_MISSING_EXTRA_HINT.format(missing=", ".join(missing)))


def register(group: click.Group) -> None:
    """Add the ``web`` subcommand (called by the CLI's entry-point discovery).

    ``group`` is the CLI's Click group (ADR-0022). Registered unconditionally:
    the subcommand is visible in ``--help`` even without the extra, and running
    it explains how to install the extra.
    """

    @group.command(
        name="web",
        help="start the local web console (projects, glossary) in a browser",
    )
    @click.option(
        "--host", default=DEFAULT_HOST, help="bind address (default localhost)"
    )
    @click.option("--port", type=int, default=DEFAULT_PORT, help="port (default 8765)")
    @click.option("--no-browser", is_flag=True, help="do not open a browser window")
    @click.option(
        "--data-dir",
        default=None,
        envvar="CR_DATA_DIR",
        show_envvar=True,
        help="override the app data directory (default: CR_DATA_DIR / XDG)",
    )
    @click.option(
        "--tailscale",
        is_flag=True,
        help=(
            "set up Tailscale Serve for this port, trust this machine's tailnet "
            "name, and print its https URL. Serve runs in the foreground "
            "alongside the console and stops with it. The tailnet is the "
            "authentication: anyone on your tailnet can reach the console."
        ),
    )
    @click.option(
        "--tailscale-host",
        default=None,
        metavar="NAME",
        help=(
            "trust NAME instead of the machine's resolved tailnet name "
            "(requires --tailscale)"
        ),
    )
    @click.option(
        "--tailscale-port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "the tailnet HTTPS port Serve exposes (default: the same as "
            "--port); requires --tailscale"
        ),
    )
    def _web(
        host: str,
        port: int,
        no_browser: bool,
        data_dir: str | None,
        tailscale: bool,
        tailscale_host: str | None,
        tailscale_port: int | None,
    ) -> int:
        return _run(
            host=host,
            port=port,
            no_browser=no_browser,
            data_dir=data_dir,
            tailscale=tailscale,
            tailscale_host=tailscale_host,
            tailscale_port=tailscale_port,
        )


def _run(
    *,
    host: str,
    port: int,
    no_browser: bool,
    data_dir: str | None,
    tailscale: bool = False,
    tailscale_host: str | None = None,
    tailscale_port: int | None = None,
) -> int:
    _require_web_stack()
    if (tailscale_host is not None or tailscale_port is not None) and not tailscale:
        raise click.UsageError(
            "--tailscale-host / --tailscale-port configure Serve; pass "
            "--tailscale too (or set CR_TRUSTED_HOSTS to trust a hostname "
            "without Serve)."
        )
    if tailscale_port is not None and not 0 < tailscale_port < 65536:
        raise click.UsageError(
            f"--tailscale-port {tailscale_port} is not a port number (1-65535)."
        )
    trusted_hosts: list[str] | None = None
    session: ServeSession | None = None
    if tailscale:
        _require_loopback_bind(host)
        trusted_hosts, session = _tailscale_setup(
            target_port=port,
            serve_port=tailscale_port if tailscale_port is not None else port,
            override=tailscale_host,
        )
    from clear_record.web.app import serve

    if session is not None:
        atexit.register(session.stop)
    restore = _guard_termination(session)
    try:
        return serve(
            host=host,
            port=port,
            open_browser=not no_browser,
            data_dir=data_dir,
            trusted_hosts=trusted_hosts,
        )
    finally:
        if restore is not None:
            restore()
        if session is not None:
            session.stop()


def _guard_termination(session: ServeSession | None):
    """Stop the Serve child on a signal that ends us without unwinding.

    ``SIGINT`` arrives as ``KeyboardInterrupt`` and is handled by the ``finally``
    around the console. ``SIGTERM``'s default action ends the process **without**
    running it, so the child would be orphaned and its mapping would outlive the
    console; a handler is the only way to stop it. uvicorn installs its own
    handlers for the duration of the run and re-raises the signal afterwards
    (restoring ours), so this fires whether or not uvicorn intercepts it.

    Returns a function that puts the previous handlers back (for a normal run),
    or ``None`` when nothing was installed — no session, no such signal, or a
    non-main thread, where ``signal.signal`` is not allowed.
    """
    if session is None or threading.current_thread() is not threading.main_thread():
        return None
    signals = [
        signum
        for name in ("SIGTERM", "SIGHUP")
        if (signum := getattr(signal, name, None)) is not None
    ]
    if not signals:
        return None

    def stop_and_terminate(number, _frame):
        session.stop()
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)

    previous = {number: signal.signal(number, stop_and_terminate) for number in signals}

    def restore() -> None:
        for number, handler in previous.items():
            signal.signal(number, handler)

    return restore


def _require_loopback_bind(host: str) -> None:
    """Reject a bind Serve could not reach, before anything is set up.

    Tailscale Serve proxies **only** to ``http://127.0.0.1:<port>`` (ADR-0021),
    so a non-loopback ``--host`` would publish a target that 502s to the whole
    tailnet. The loopback notion is the request guard's own
    :func:`clear_record.web.guard.is_loopback_host` (via ``host_name``), so the
    bind check and the guard agree on what "loopback" means — no second list.
    """
    from clear_record.web import guard

    if not guard.is_loopback_host(guard.host_name(host)):
        raise click.UsageError(
            "--tailscale sets up Tailscale Serve against "
            f"http://127.0.0.1:<port>, but --host {host!r} is not a loopback "
            "address, so Serve could not reach the console.\n"
            "  Drop --host (the console binds 127.0.0.1 by default), or drop "
            "--tailscale and front the loopback console with your own proxy "
            "(ADR-0021)."
        )


def _tailscale_setup(
    *, target_port: int, serve_port: int, override: str | None
) -> tuple[list[str], ServeSession | None]:
    """Set up Serve, echo what happened, return ``(trusted_hosts, session)``.

    Name resolution is required — without it there is nothing to trust — so it
    stays fatal. Starting Serve is not: the flag is a convenience, so a refusal
    becomes an actionable warning and the console starts anyway.

    ``CR_TRUSTED_HOSTS`` still composes: the tailnet name is added to it, so a
    console already served on a public name keeps working. No environment
    variable is set or needed — the name is passed straight to ``create_app``.
    """
    from clear_record.web import guard, tailscale

    try:
        name = tailscale.normalize_name(override) or tailscale.resolve_dns_name()
        if not name or "/" in name:
            raise tailscale.TailscaleError(
                f"--tailscale-host {override!r} is not a bare hostname.\n"
                "    Fix: pass the name Tailscale gives you, e.g. "
                "machine.tailnet.ts.net."
            )
    except tailscale.TailscaleError as exc:
        raise SystemExit(f"[tailscale] {exc}") from exc

    try:
        session = tailscale.start_serve(serve_port=serve_port, target_port=target_port)
    except tailscale.TailscaleError as exc:
        click.echo(
            f"[tailscale] {exc}\n"
            "    The console is starting anyway, but it will not be reachable "
            "over the tailnet.",
            err=True,
        )
        session = None
    else:
        if session.reused:
            click.echo(
                f"[tailscale] port {serve_port} is already served by Tailscale; "
                "leaving that mapping untouched.\n"
                "    If it does not point at this console, pass "
                "--tailscale-port <port> to expose a different tailnet port."
            )
        else:
            click.echo(
                "[tailscale] console is now shared on your tailnet:\n"
                f"    {tailscale.console_url(name, serve_port)}\n"
                "    The tailnet is the authentication: anyone on your tailnet "
                "can reach this console.\n"
                "    Serve runs in the foreground and stops with this console."
            )
    return sorted(guard.trusted_extra_hosts() | {name}), session


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "register"]
