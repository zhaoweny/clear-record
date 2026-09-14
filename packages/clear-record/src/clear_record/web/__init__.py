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

from clear_record.core.i18n import deferred, tr

if TYPE_CHECKING:
    from clear_record.web.tailscale import ServeSession

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Everything the console needs at run time. `clear_record.web` itself is always
#: importable (it ships in the one wheel); these are the optional `web` extra.
_WEB_STACK = ("fastapi", "uvicorn", "jinja2", "python_multipart")

_MISSING_EXTRA_HINT = deferred(
    "[web] the web console needs the optional 'web' extra ({missing} not found).\n"
    "  Install it with:   pip install 'clear-record[web]'\n"
    "  Or run without installing:   uvx --from 'clear-record[web]' clear-record web"
)


def _require_web_stack() -> None:
    """Fail with an actionable hint when the `web` extra is not installed."""
    missing = [name for name in _WEB_STACK if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit(tr(_MISSING_EXTRA_HINT, missing=", ".join(missing)))


#: The bind/data/Tailscale options both console commands share. A helper keeps
#: `web` and `serve` from drifting: they differ only in browser/logging posture.
_COMMON_CONSOLE_OPTIONS = (
    click.option(
        "--host",
        default=DEFAULT_HOST,
        help=tr("bind address (default localhost)"),
    ),
    click.option(
        "--port", type=int, default=DEFAULT_PORT, help=tr("port (default 8765)")
    ),
    click.option(
        "--data-dir",
        default=None,
        envvar="CR_DATA_DIR",
        show_envvar=True,
        help=tr(
            "override the app data directory "
            "(default: CR_DATA_DIR / the platform data directory)"
        ),
    ),
    click.option(
        "--tailscale",
        is_flag=True,
        help=tr(
            "set up Tailscale Serve for this port, trust this machine's tailnet "
            "name, and print its https URL. Serve runs in the foreground "
            "alongside the console and stops with it. The tailnet is the "
            "authentication: anyone on your tailnet can reach the console."
        ),
    ),
    click.option(
        "--tailscale-host",
        default=None,
        metavar="NAME",
        help=tr(
            "trust NAME instead of the machine's resolved tailnet name "
            "(requires --tailscale)"
        ),
    ),
    click.option(
        "--tailscale-port",
        type=int,
        default=None,
        metavar="PORT",
        help=tr(
            "the tailnet HTTPS port Serve exposes (default: the same as "
            "--port); requires --tailscale"
        ),
    ),
)


def _console_options(*extra):
    """Compose the shared console options plus any command-specific ones.

    Click decorators apply bottom-up; composing them here keeps `web` and
    `serve` on one option table (ADR-0013's "one dist, one console"), so the two
    commands cannot drift into two behaviours.
    """

    def apply(fn):
        for decorator in reversed(extra + _COMMON_CONSOLE_OPTIONS):
            fn = decorator(fn)
        return fn

    return apply


def register(group: click.Group) -> None:
    """Add the ``web`` and ``serve`` subcommands (CLI entry-point discovery).

    ``group`` is the CLI's Click group (ADR-0022). Registered unconditionally:
    the subcommands are visible in ``--help`` even without the extra, and running
    one explains how to install the extra.

    They are **two postures of one console, not two names for one behaviour**:

    * ``web`` is the *interactive* entry point — it opens a browser by default and
      logs to the terminal, for a person at the machine.
    * ``serve`` is the *node* entry point ADR-0013 promised — headless (no
      browser), its logs go to the diagnostics sink, and it is what a
      systemd/launchd unit runs. The queue and startup reconciliation it drives
      are the service's, not a second implementation.

    ``tr`` runs here, at group-build time, not at import: the catalog is
    installed before the group is assembled (``--lang``/``CR_LANG``/``LANG``),
    so the help text is translated while the module stays import-light.
    """

    @group.command(
        name="web",
        help=tr("start the local web console (projects, glossary) in a browser"),
    )
    @_console_options(
        click.option(
            "--no-browser", is_flag=True, help=tr("do not open a browser window")
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

    @group.command(
        name="serve",
        help=tr(
            "run the headless console for a service supervisor: no browser, "
            "logs to the diagnostics sink, one run per node"
        ),
    )
    @_console_options()
    def _serve(
        host: str,
        port: int,
        data_dir: str | None,
        tailscale: bool,
        tailscale_host: str | None,
        tailscale_port: int | None,
    ) -> int:
        return _run(
            host=host,
            port=port,
            no_browser=True,
            data_dir=data_dir,
            tailscale=tailscale,
            tailscale_host=tailscale_host,
            tailscale_port=tailscale_port,
            service=True,
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
    service: bool = False,
) -> int:
    _require_web_stack()
    if (tailscale_host is not None or tailscale_port is not None) and not tailscale:
        raise click.UsageError(
            tr(
                "--tailscale-host / --tailscale-port configure Serve; pass "
                "--tailscale too (or set CR_TRUSTED_HOSTS to trust a hostname "
                "without Serve)."
            )
        )
    if tailscale_port is not None and not 0 < tailscale_port < 65536:
        raise click.UsageError(
            tr(
                "--tailscale-port {port} is not a port number (1-65535).",
                port=tailscale_port,
            )
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
    # `serve` (the node entry point) routes the console's logs to the diagnostics
    # sink; `web` (interactive) keeps uvicorn's terminal logging.
    serve_kwargs: dict = {}
    if service:
        from clear_record.core.diagnostics import log_event
        from clear_record.service.diagnostics import console_log_config

        serve_kwargs["log_config"] = console_log_config()
        log_event(
            "info",
            "console",
            "console.serving",
            host=host,
            port=port,
            data_dir=data_dir or "",
        )
    try:
        return serve(
            host=host,
            port=port,
            open_browser=not no_browser,
            data_dir=data_dir,
            trusted_hosts=trusted_hosts,
            **serve_kwargs,
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
            tr(
                "--tailscale sets up Tailscale Serve against "
                "http://127.0.0.1:<port>, but --host {host!r} is not a loopback "
                "address, so Serve could not reach the console.\n"
                "  Drop --host (the console binds 127.0.0.1 by default), or drop "
                "--tailscale and front the loopback console with your own proxy "
                "(ADR-0021).",
                host=host,
            )
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
                tr(
                    "--tailscale-host {override!r} is not a bare hostname.\n"
                    "    Fix: pass the name Tailscale gives you, e.g. "
                    "machine.tailnet.ts.net.",
                    override=override,
                )
            )
    except tailscale.TailscaleError as exc:
        raise SystemExit(f"[tailscale] {exc}") from exc

    try:
        session = tailscale.start_serve(serve_port=serve_port, target_port=target_port)
    except tailscale.TailscaleError as exc:
        click.echo(
            tr(
                "[tailscale] {error}\n"
                "    The console is starting anyway, but it will not be reachable "
                "over the tailnet.",
                error=exc,
            ),
            err=True,
        )
        session = None
    else:
        if session.reused:
            click.echo(
                tr(
                    "[tailscale] port {port} is already served by Tailscale; "
                    "leaving that mapping untouched.\n"
                    "    If it does not point at this console, pass "
                    "--tailscale-port <port> to expose a different tailnet port.",
                    port=serve_port,
                )
            )
        else:
            click.echo(
                tr(
                    "[tailscale] console is now shared on your tailnet:\n"
                    "    {url}\n"
                    "    The tailnet is the authentication: anyone on your tailnet "
                    "can reach this console.\n"
                    "    Serve runs in the foreground and stops with this console.",
                    url=tailscale.console_url(name, serve_port),
                )
            )
    return sorted(guard.trusted_extra_hosts() | {name}), session


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "register"]
