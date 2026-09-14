"""``clear-record web --tailscale``: resolve, Serve, trust, print (ticket 01).

No real Tailscale is needed: every test fakes the ``tailscale`` subprocess by
replacing :data:`clear_record.web.tailscale.subprocess`. The console server is
never started — ``clear_record.web.app.serve`` is replaced with a recorder, then
the trusted hosts it received are exercised against a real :class:`TestClient`.

The security shape under test: the tailnet name is trusted **because it was
passed to ``create_app``**, with no environment variable set — the design
decision that replaced the parent-child ``CR_TRUSTED_HOSTS`` handoff.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from clear_record.service import Registry
from clear_record.web import tailscale
from clear_record.web.app import create_app

PORT = 8765
TAILNET_NAME = "myhost.tailnet.ts.net"


def _completed(*args: str, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _status_json(dns_name: object = f"{TAILNET_NAME}.", **overrides: object) -> str:
    payload: dict = {
        "BackendState": "Running",
        "Self": {"DNSName": dns_name, "HostName": "myhost"},
        "CurrentTailnet": {"MagicDNSSuffix": "tailnet.ts.net", "MagicDNSEnabled": True},
    }
    payload.update(overrides)
    return json.dumps(payload)


class _FakeTailscale:
    """Records each ``tailscale ...`` argv and replays canned results."""

    def __init__(self, *, status=None, serve=None, missing: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._status = status
        self._serve = serve
        self._missing = missing

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        if self._missing:
            raise FileNotFoundError(cmd[0])
        if cmd[1] == "status":
            return self._status
        return self._serve

    def subcommands(self) -> list[str]:
        return [call[1] for call in self.calls]


@pytest.fixture()
def fake_tailscale(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``subprocess.run`` guarding ``tailscale``; returns it."""

    def install(*, status=None, serve=None, missing: bool = False) -> _FakeTailscale:
        fake = _FakeTailscale(status=status, serve=serve, missing=missing)
        monkeypatch.setattr(tailscale.subprocess, "run", fake)
        return fake

    return install


@pytest.fixture()
def captured_serve(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the uvicorn launcher with a recorder of its ``trusted_hosts``."""
    captured: dict = {}

    def fake_serve(*, host, port, open_browser, data_dir=None, trusted_hosts=None):
        captured.update(
            host=host,
            port=port,
            open_browser=open_browser,
            data_dir=data_dir,
            trusted_hosts=trusted_hosts,
        )
        return 0

    monkeypatch.setattr("clear_record.web.app.serve", fake_serve)
    return captured


def _web_command():
    from clear_record.cli import cli

    return cli._build_group().commands["web"]


# --- resolving the tailnet name ------------------------------------------- #
def test_resolve_reads_dns_name_and_normalises_the_trailing_dot(fake_tailscale) -> None:
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json())
    )
    assert tailscale.resolve_dns_name() == TAILNET_NAME
    assert fake.calls == [["tailscale", "status", "--json"]]


def test_resolve_falls_back_to_hostname_plus_magicdns_suffix(fake_tailscale) -> None:
    payload = json.dumps(
        {
            "BackendState": "Running",
            "Self": {"HostName": "myhost"},
            "CurrentTailnet": {"MagicDNSSuffix": "tailnet.ts.net"},
        }
    )
    fake_tailscale(status=_completed("tailscale", "status", stdout=payload))
    assert tailscale.resolve_dns_name() == TAILNET_NAME


def test_resolve_missing_binary_names_the_install_fix(fake_tailscale) -> None:
    fake_tailscale(missing=True)
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    message = str(excinfo.value)
    assert "could not run `tailscale`" in message
    assert "PATH" in message


def test_resolve_daemon_down_surfaces_stderr_and_the_fix(fake_tailscale) -> None:
    fake_tailscale(
        status=_completed(
            "tailscale",
            "status",
            returncode=1,
            stderr="The Tailscale GUI is not running.",
        )
    )
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    message = str(excinfo.value)
    assert "The Tailscale GUI is not running." in message
    assert "tailscale up" in message


def test_resolve_needs_login_is_actionable(fake_tailscale) -> None:
    fake_tailscale(
        status=_completed(
            "tailscale", "status", stdout=_status_json(BackendState="NeedsLogin")
        )
    )
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    assert "NeedsLogin" in str(excinfo.value)


def test_resolve_rejects_non_json_output(fake_tailscale) -> None:
    fake_tailscale(status=_completed("tailscale", "status", stdout="not json"))
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    assert "did not return JSON" in str(excinfo.value)


def test_resolve_rejects_an_unexpected_json_shape(fake_tailscale) -> None:
    fake_tailscale(status=_completed("tailscale", "status", stdout="[]"))
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    assert "unexpected shape" in str(excinfo.value)


def test_resolve_without_any_dns_name_points_at_magicdns(fake_tailscale) -> None:
    payload = json.dumps({"BackendState": "Running", "Self": {}})
    fake_tailscale(status=_completed("tailscale", "status", stdout=payload))
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    message = str(excinfo.value)
    assert "MagicDNS" in message
    assert "--tailscale-host" in message


# --- running Serve --------------------------------------------------------- #
def test_serve_runs_the_persistent_background_form(fake_tailscale) -> None:
    fake = fake_tailscale(serve=_completed("tailscale", "serve", returncode=0))
    assert tailscale.serve(PORT) == f"http://127.0.0.1:{PORT}"
    assert fake.calls == [["tailscale", "serve", "--bg", f"http://127.0.0.1:{PORT}"]]


def test_serve_refusal_surfaces_the_cli_stderr(fake_tailscale) -> None:
    fake_tailscale(
        serve=_completed(
            "tailscale",
            "serve",
            returncode=1,
            stderr="Serve is not enabled on your tailnet",
        )
    )
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.serve(PORT)
    message = str(excinfo.value)
    assert "Serve is not enabled on your tailnet" in message
    assert "HTTPS" in message


def test_normalize_name_handles_urls_case_and_dots() -> None:
    assert tailscale.normalize_name("Host.Tailnet.ts.net.") == "host.tailnet.ts.net"
    assert (
        tailscale.normalize_name("https://host.tailnet.ts.net/")
        == "host.tailnet.ts.net"
    )
    assert tailscale.normalize_name(None) == ""


def test_disable_hint_scopes_to_the_mapping_not_reset() -> None:
    # `tailscale serve reset` would clear every rule on the machine; the protocol
    # flag removes only the default HTTPS mapping this flag created.
    assert tailscale.disable_hint() == "tailscale serve --https=443 off"


# --- the whole flag: serve + trust + print --------------------------------- #
def test_tailscale_flag_serves_trusts_and_prints_the_url(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        serve=_completed("tailscale", "serve"),
    )

    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert captured_serve["trusted_hosts"] == [TAILNET_NAME]
    assert f"https://{TAILNET_NAME}/" in result.output
    assert "anyone on your tailnet" in result.output
    assert tailscale.SERVE_OFF in result.output
    assert ["tailscale", "serve", "--bg", f"http://127.0.0.1:{PORT}"] in fake.calls


def test_tailscale_host_override_wins_and_skips_resolution(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed(
            "tailscale", "status", stdout=_status_json("resolved.ts.net.")
        ),
        serve=_completed("tailscale", "serve"),
    )

    result = CliRunner().invoke(
        _web_command(),
        ["--tailscale", "--tailscale-host", "Custom.Tailnet.ts.net", "--no-browser"],
    )

    assert result.exit_code == 0, result.output
    assert fake.subcommands() == ["serve"]  # status was never consulted
    assert captured_serve["trusted_hosts"] == ["custom.tailnet.ts.net"]
    assert "https://custom.tailnet.ts.net/" in result.output


def test_tailscale_host_without_tailscale_is_a_usage_error(captured_serve) -> None:
    result = CliRunner().invoke(
        _web_command(), ["--tailscale-host", "x.ts.net", "--no-browser"]
    )
    assert result.exit_code != 0
    assert "--tailscale" in result.output
    assert captured_serve == {}


def test_tailscale_rejects_a_non_loopback_bind(fake_tailscale, captured_serve) -> None:
    """Serve proxies only to loopback, so a non-loopback bind would 502."""
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        serve=_completed("tailscale", "serve"),
    )
    result = CliRunner().invoke(
        _web_command(), ["--tailscale", "--host", "0.0.0.0", "--no-browser"]
    )

    assert result.exit_code != 0
    assert "0.0.0.0" in result.output
    assert "loopback" in result.output
    assert "127.0.0.1" in result.output
    assert fake.calls == []  # neither `status` nor `serve` ran
    assert captured_serve == {}  # and the console never started


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_tailscale_accepts_loopback_binds(
    fake_tailscale, captured_serve, monkeypatch, host: str
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        serve=_completed("tailscale", "serve"),
    )
    result = CliRunner().invoke(
        _web_command(), ["--tailscale", "--host", host, "--no-browser"]
    )

    assert result.exit_code == 0, result.output
    assert captured_serve["host"] == host
    assert captured_serve["trusted_hosts"] == [TAILNET_NAME]
    assert ["tailscale", "serve", "--bg", f"http://127.0.0.1:{PORT}"] in fake.calls


def test_tailscale_composes_with_an_explicit_trusted_hosts_env(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.setenv("CR_TRUSTED_HOSTS", "console.example.com")
    fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        serve=_completed("tailscale", "serve"),
    )

    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert captured_serve["trusted_hosts"] == ["console.example.com", TAILNET_NAME]


def test_no_env_var_is_needed_for_the_host_to_be_trusted(
    fake_tailscale, captured_serve, monkeypatch, tmp_path: Path
) -> None:
    """The resolved name is passed directly — not via CR_TRUSTED_HOSTS."""
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        serve=_completed("tailscale", "serve"),
    )
    assert (
        CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"]).exit_code
        == 0
    )

    app = create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"),
        trusted_hosts=captured_serve["trusted_hosts"],
    )
    client = TestClient(app, base_url=f"https://{TAILNET_NAME}")
    res = client.post(
        "/ui/projects",
        data={"name": "Tailnet"},
        headers={"host": TAILNET_NAME, "Origin": f"https://{TAILNET_NAME}"},
    )
    assert res.status_code == 200


# --- failures through the CLI are messages, not tracebacks ----------------- #
def test_cli_reports_a_missing_tailscale_without_a_traceback(
    fake_tailscale, captured_serve
) -> None:
    fake_tailscale(missing=True)
    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])
    assert result.exit_code == 1
    assert "[tailscale]" in result.output
    assert "PATH" in result.output
    assert "Traceback" not in result.output
    assert captured_serve == {}  # the console never started


def test_cli_surfaces_a_refused_serve_and_does_not_start(
    fake_tailscale, captured_serve
) -> None:
    fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        serve=_completed(
            "tailscale", "serve", returncode=1, stderr="serve: access denied"
        ),
    )
    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])
    assert result.exit_code == 1
    assert "serve: access denied" in result.output
    assert "Traceback" not in result.output
    assert captured_serve == {}


def test_help_states_the_security_shape() -> None:
    result = CliRunner().invoke(_web_command(), ["--help"])
    assert result.exit_code == 0
    assert "--tailscale" in result.output
    assert "tailnet is the authentication" in result.output
