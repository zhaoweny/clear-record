"""The bundled web surface for clear-record.

Importing this package is **deliberately light**: it must not import FastAPI or
uvicorn, because the CLI discovers the ``web`` subcommand through an entry point
while building the parser — and a plain `clear-record ingest` should not pay the
web stack's import cost. The app is imported inside the handler.

The subcommand is registered through the ``clear_record.commands`` entry-point
group (ADR-0013) so ``clear_record.cli`` never statically imports this module.
"""

from __future__ import annotations

import importlib.util

import click

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
            "name, and print its https URL. The tailnet is the authentication: "
            "anyone on your tailnet can reach the console. Serve is left running "
            "on purpose; turn it off with `tailscale serve off`."
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
    def _web(
        host: str,
        port: int,
        no_browser: bool,
        data_dir: str | None,
        tailscale: bool,
        tailscale_host: str | None,
    ) -> int:
        return _run(
            host=host,
            port=port,
            no_browser=no_browser,
            data_dir=data_dir,
            tailscale=tailscale,
            tailscale_host=tailscale_host,
        )


def _run(
    *,
    host: str,
    port: int,
    no_browser: bool,
    data_dir: str | None,
    tailscale: bool = False,
    tailscale_host: str | None = None,
) -> int:
    _require_web_stack()
    trusted_hosts: list[str] | None = None
    if tailscale_host and not tailscale:
        raise click.UsageError(
            "--tailscale-host overrides the name --tailscale resolves; pass "
            "--tailscale too (or set CR_TRUSTED_HOSTS to trust a hostname "
            "without Serve)."
        )
    if tailscale:
        _require_loopback_bind(host)
        trusted_hosts = _tailscale_hosts(port=port, override=tailscale_host)
    from clear_record.web.app import serve

    return serve(
        host=host,
        port=port,
        open_browser=not no_browser,
        data_dir=data_dir,
        trusted_hosts=trusted_hosts,
    )


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


def _tailscale_hosts(*, port: int, override: str | None) -> list[str]:
    """Set up Serve, echo the URL and turn-off hint, return the names to trust.

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
        tailscale.serve(port)
    except tailscale.TailscaleError as exc:
        raise SystemExit(f"[tailscale] {exc}") from exc

    click.echo(
        "[tailscale] console is now shared on your tailnet:\n"
        f"    {tailscale.console_url(name)}\n"
        "    The tailnet is the authentication: anyone on your tailnet can "
        "reach this console.\n"
        f"    To stop sharing it, run:  {tailscale.disable_hint()}"
    )
    return sorted(guard.trusted_extra_hosts() | {name})


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "register"]
