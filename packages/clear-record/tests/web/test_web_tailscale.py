"""``clear-record web --tailscale``: resolve, Serve, trust, print, tear down.

No real Tailscale is needed: every test fakes the ``tailscale`` subprocess by
replacing the ``run`` and ``Popen`` the module reaches for. The console server is
never started — ``clear_record.web.app.serve`` is replaced with a recorder, then
the trusted hosts it received are exercised against a real :class:`TestClient`.

The two shapes under test:

- the tailnet name is trusted **because it was passed to ``create_app``**, with no
  environment variable set (the design decision that replaced the parent-child
  ``CR_TRUSTED_HOSTS`` handoff);
- Serve runs in the **foreground** as a real child, so killing that child (on
  console exit, SIGINT/SIGTERM or otherwise) is what removes the mapping. A rule
  that was already there is snapshotted first and left untouched.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from clear_record.service import Registry
from clear_record.web import _guard_termination, tailscale
from clear_record.web.app import create_app

PORT = 8765
EXPOSED_PORT = 443
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


def _serve_status(**config: object) -> subprocess.CompletedProcess:
    """A ``serve status --json`` result carrying ``config`` (the raw ServeConfig)."""
    return _completed("tailscale", "serve", "status", stdout=json.dumps(config))


class _FakeProcess:
    """A fake foreground ``tailscale serve`` child.

    ``exit_code=None`` models a running Serve: the startup grace wait times out,
    and only ``terminate()`` (or ``kill()``) ends it. A non-None code models a
    refusal that exits immediately.
    """

    def __init__(
        self, *, exit_code: int | None = None, stderr: str = "", stdout: str = ""
    ) -> None:
        self.returncode = exit_code
        self.stderr = io.StringIO(stderr)
        self.stdout = io.StringIO(stdout)
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="tailscale serve", timeout=timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class _StubbornProcess(_FakeProcess):
    """A child that ignores SIGTERM, so cleanup must escalate to SIGKILL."""

    def terminate(self) -> None:
        self.terminate_calls += 1  # stay running


class _FakeTailscale:
    """Records one-shot calls and spawns and replays canned results."""

    def __init__(
        self,
        *,
        status=None,
        serve_status=None,
        missing: bool = False,
        children: list[_FakeProcess] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.popen_calls: list[list[str]] = []
        self.processes: list[_FakeProcess] = []
        self._status = status
        self._serve_status = serve_status
        self._missing = missing
        self._children = list(children or [])

    def run(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        if self._missing:
            raise FileNotFoundError(cmd[0])
        if cmd[1] == "status":
            return self._status
        if cmd[1] == "serve" and cmd[2] == "status":
            return self._serve_status
        raise AssertionError(f"unexpected one-shot tailscale call: {cmd}")

    def popen(self, cmd, **_kwargs):
        self.popen_calls.append(list(cmd))
        if self._missing:
            raise FileNotFoundError(cmd[0])
        process = self._children.pop(0) if self._children else _FakeProcess()
        self.processes.append(process)
        return process

    def subcommands(self) -> list[str]:
        return [call[1] for call in self.calls]

    def spawned(self) -> list[list[str]]:
        return [list(call) for call in self.popen_calls]


@pytest.fixture()
def fake_tailscale(monkeypatch: pytest.MonkeyPatch):
    """Install fakes for ``subprocess.run``/``Popen``; returns the installer."""

    # Binary discovery is host-dependent (this machine's `tailscale` is a
    # symlink into the app bundle), so pin it to the bare name here: these tests
    # assert the subcommands and ports, not discovery. Resolution itself is
    # covered in ``test_web_tailscale_binary.py``.
    monkeypatch.setattr(tailscale.shutil, "which", lambda _name: None)
    monkeypatch.delenv(tailscale.TAILSCALE_BIN_ENV, raising=False)

    def install(
        *,
        status=None,
        serve_status=None,
        missing: bool = False,
        children: list[_FakeProcess] | None = None,
    ) -> _FakeTailscale:
        if serve_status is None:
            serve_status = _serve_status()
        fake = _FakeTailscale(
            status=status,
            serve_status=serve_status,
            missing=missing,
            children=children,
        )
        monkeypatch.setattr(tailscale.subprocess, "run", fake.run)
        monkeypatch.setattr(tailscale.subprocess, "Popen", fake.popen)
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


def test_normalize_name_handles_urls_case_and_dots() -> None:
    assert tailscale.normalize_name("Host.Tailnet.ts.net.") == "host.tailnet.ts.net"
    assert (
        tailscale.normalize_name("https://host.tailnet.ts.net/")
        == "host.tailnet.ts.net"
    )
    assert tailscale.normalize_name(None) == ""


# --- snapshotting what Serve already serves -------------------------------- #
def test_read_served_ports_returns_empty_for_a_blank_config(fake_tailscale) -> None:
    fake_tailscale(serve_status=_serve_status())
    assert tailscale.read_served_ports() == frozenset()


@pytest.mark.parametrize(
    "config",
    [
        {"TCP": {"443": {"HTTPS": True}}},
        {"Web": {f"{TAILNET_NAME}:443": {"Handlers": {"/": {"Proxy": "x"}}}}},
        {"Foreground": {"session": {"TCP": {"443": {"HTTPS": True}}}}},
        {"Foreground": {"session": {"Web": {f"{TAILNET_NAME}:443": {"Handlers": {}}}}}},
    ],
)
def test_served_ports_reads_tcp_web_and_foreground_mappings(config) -> None:
    assert tailscale.served_ports(config) == frozenset({443})


def test_served_ports_reads_ipv6_web_keys_and_ignores_services() -> None:
    config = {
        "Web": {"[fd7a:115c:a1e0::1]:8443": {"Handlers": {}}},
        "Services": {"svc:web": {"TCP": {"443": {"HTTPS": True}}}},
    }
    assert tailscale.served_ports(config) == frozenset({8443})


def test_read_served_ports_is_empty_when_status_cannot_be_read(fake_tailscale) -> None:
    fake_tailscale(
        serve_status=_completed(
            "tailscale", "serve", "status", returncode=1, stderr="not running"
        )
    )
    assert tailscale.read_served_ports() == frozenset()


# --- starting foreground Serve --------------------------------------------- #
def test_start_serve_spawns_the_foreground_form_with_the_same_port(
    fake_tailscale,
) -> None:
    fake = fake_tailscale()
    session = tailscale.start_serve(serve_port=PORT, target_port=PORT)

    assert session.started is True
    assert session.reused is False
    assert fake.spawned() == [
        ["tailscale", "serve", f"--https={PORT}", f"http://127.0.0.1:{PORT}"]
    ]
    # Foreground means no `--bg`: the mapping dies with the child.
    assert "--bg" not in fake.spawned()[0]


def test_start_serve_names_the_exposed_port_and_keeps_the_target_loopback(
    fake_tailscale,
) -> None:
    fake = fake_tailscale()
    tailscale.start_serve(serve_port=EXPOSED_PORT, target_port=PORT)
    assert fake.spawned() == [
        ["tailscale", "serve", "--https=443", f"http://127.0.0.1:{PORT}"]
    ]


def test_start_serve_reuses_a_mapping_that_pre_existed_and_spawns_nothing(
    fake_tailscale,
) -> None:
    fake = fake_tailscale(
        serve_status=_serve_status(
            TCP={str(PORT): {"HTTPS": True}},
            Web={f"{TAILNET_NAME}:{PORT}": {"Handlers": {"/": {"Proxy": "x"}}}},
        )
    )
    session = tailscale.start_serve(serve_port=PORT, target_port=PORT)

    assert session.started is False
    assert session.reused is True
    assert fake.spawned() == []
    assert fake.processes == []


def test_start_serve_reuses_a_mapping_in_another_foreground_session(
    fake_tailscale,
) -> None:
    fake = fake_tailscale(
        serve_status=_serve_status(
            Foreground={"other": {"TCP": {str(PORT): {"HTTPS": True}}}}
        )
    )
    session = tailscale.start_serve(serve_port=PORT, target_port=PORT)
    assert session.reused is True
    assert fake.spawned() == []


def test_start_serve_surfaces_a_refusal_with_the_fix(fake_tailscale) -> None:
    fake_tailscale(
        children=[
            _FakeProcess(exit_code=1, stderr="Serve is not enabled on your tailnet")
        ]
    )
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.start_serve(serve_port=PORT, target_port=PORT)
    message = str(excinfo.value)
    assert "Serve is not enabled on your tailnet" in message
    assert "HTTPS" in message
    assert "--tailscale-port" in message


def test_start_serve_reads_a_refusal_from_stdout_when_stderr_is_empty(
    fake_tailscale,
) -> None:
    fake_tailscale(children=[_FakeProcess(exit_code=2, stdout="usage: tailscale")])
    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.start_serve(serve_port=PORT, target_port=PORT)
    assert "usage: tailscale" in str(excinfo.value)


# --- tearing the child down ------------------------------------------------ #
def test_stop_terminates_the_child_and_is_idempotent(fake_tailscale) -> None:
    fake = fake_tailscale()
    session = tailscale.start_serve(serve_port=PORT, target_port=PORT)
    process = fake.processes[0]

    session.stop()
    assert process.terminate_calls == 1
    assert process.kill_calls == 0

    session.stop()  # the atexit backstop may fire after the finally
    assert process.terminate_calls == 1


def test_stop_escalates_to_kill_when_the_child_ignores_sigterm(
    fake_tailscale,
) -> None:
    fake = fake_tailscale(children=[_StubbornProcess()])
    session = tailscale.start_serve(serve_port=PORT, target_port=PORT)
    process = fake.processes[0]

    session.stop()
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_stop_on_a_reused_mapping_touches_nothing(fake_tailscale) -> None:
    fake = fake_tailscale(serve_status=_serve_status(TCP={str(PORT): {"HTTPS": True}}))
    session = tailscale.start_serve(serve_port=PORT, target_port=PORT)
    session.stop()  # must be a no-op: we created no child
    assert fake.processes == []


def test_console_url_omits_the_default_https_port() -> None:
    assert tailscale.console_url("Host.Tailnet.ts.net.", 443) == (
        "https://host.tailnet.ts.net/"
    )


def test_console_url_spells_out_a_non_default_port() -> None:
    assert tailscale.console_url(TAILNET_NAME, 8765) == (
        "https://myhost.tailnet.ts.net:8765/"
    )


# --- the whole flag: serve + trust + print --------------------------------- #
def test_tailscale_flag_serves_trusts_and_prints_the_url(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json())
    )

    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert captured_serve["trusted_hosts"] == [TAILNET_NAME]
    assert f"https://{TAILNET_NAME}:{PORT}/" in result.output
    assert "anyone on your tailnet" in result.output
    assert "stops with this console" in result.output
    assert fake.spawned() == [
        ["tailscale", "serve", f"--https={PORT}", f"http://127.0.0.1:{PORT}"]
    ]


def test_tailscale_port_overrides_the_exposed_port(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json())
    )

    result = CliRunner().invoke(
        _web_command(),
        ["--tailscale", "--tailscale-port", str(EXPOSED_PORT), "--no-browser"],
    )

    assert result.exit_code == 0, result.output
    assert fake.spawned() == [
        ["tailscale", "serve", "--https=443", f"http://127.0.0.1:{PORT}"]
    ]
    # 443 is implicit in an HTTPS URL.
    assert f"https://{TAILNET_NAME}/" in result.output


def test_cleanup_runs_on_the_console_exit_path(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json())
    )

    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert len(fake.processes) == 1
    assert fake.processes[0].terminate_calls == 1  # the finally tore it down


def test_cleanup_runs_when_the_console_exits_with_an_error(
    fake_tailscale, monkeypatch
) -> None:
    """SIGINT arrives as KeyboardInterrupt; the `finally` must still tear down."""
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json())
    )

    def boom(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("clear_record.web.app.serve", boom)
    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code != 0
    assert len(fake.processes) == 1
    assert fake.processes[0].terminate_calls == 1


def test_sigterm_guard_stops_the_child_and_re_raises(
    fake_tailscale, monkeypatch
) -> None:
    """SIGTERM ends the process without unwinding, so a handler must tear down."""
    fake = fake_tailscale()
    session = tailscale.start_serve(serve_port=PORT, target_port=PORT)

    installed: dict = {}

    def fake_signal(number, handler):
        previous = installed.get(number, signal.SIG_DFL)
        installed[number] = handler
        return previous

    killed: list = []
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(os, "kill", lambda pid, number: killed.append((pid, number)))

    restore = _guard_termination(session)
    handler = installed[signal.SIGTERM]
    handler(signal.SIGTERM, None)

    assert fake.processes[0].terminate_calls == 1
    assert killed[-1][1] == signal.SIGTERM  # then the signal does its usual job
    restore()  # must put the previous handlers back without raising


def test_a_pre_existing_mapping_is_left_alone(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        serve_status=_serve_status(TCP={str(PORT): {"HTTPS": True}}),
    )

    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert fake.spawned() == []  # nothing created
    assert fake.processes == []  # so nothing to tear down
    assert "already served" in result.output
    assert "leaving that mapping untouched" in result.output
    assert captured_serve["trusted_hosts"] == [TAILNET_NAME]


def test_tailscale_host_override_wins_and_skips_resolution(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake = fake_tailscale(
        status=_completed(
            "tailscale", "status", stdout=_status_json("resolved.ts.net.")
        )
    )

    result = CliRunner().invoke(
        _web_command(),
        ["--tailscale", "--tailscale-host", "Custom.Tailnet.ts.net", "--no-browser"],
    )

    assert result.exit_code == 0, result.output
    assert ["tailscale", "status", "--json"] not in fake.calls  # resolution skipped
    assert captured_serve["trusted_hosts"] == ["custom.tailnet.ts.net"]
    assert "https://custom.tailnet.ts.net:" in result.output


def test_tailscale_host_without_tailscale_is_a_usage_error(captured_serve) -> None:
    result = CliRunner().invoke(
        _web_command(), ["--tailscale-host", "x.ts.net", "--no-browser"]
    )
    assert result.exit_code != 0
    assert "--tailscale" in result.output
    assert captured_serve == {}


def test_tailscale_port_without_tailscale_is_a_usage_error(captured_serve) -> None:
    result = CliRunner().invoke(
        _web_command(), ["--tailscale-port", "443", "--no-browser"]
    )
    assert result.exit_code != 0
    assert "--tailscale" in result.output
    assert captured_serve == {}


@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_tailscale_port_must_be_a_real_port(
    fake_tailscale, captured_serve, value: str
) -> None:
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json())
    )
    result = CliRunner().invoke(
        _web_command(), ["--tailscale", "--tailscale-port", value, "--no-browser"]
    )
    assert result.exit_code != 0
    assert "port number" in result.output
    assert captured_serve == {}
    assert fake.calls == []  # rejected before any Tailscale call


def test_tailscale_rejects_a_non_loopback_bind(fake_tailscale, captured_serve) -> None:
    """Serve proxies only to loopback, so a non-loopback bind would 502."""
    fake = fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json())
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
        status=_completed("tailscale", "status", stdout=_status_json())
    )
    result = CliRunner().invoke(
        _web_command(), ["--tailscale", "--host", host, "--no-browser"]
    )

    assert result.exit_code == 0, result.output
    assert captured_serve["host"] == host
    assert captured_serve["trusted_hosts"] == [TAILNET_NAME]
    assert fake.spawned()[0][-1] == f"http://127.0.0.1:{PORT}"


def test_tailscale_composes_with_an_explicit_trusted_hosts_env(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    monkeypatch.setenv("CR_TRUSTED_HOSTS", "console.example.com")
    fake_tailscale(status=_completed("tailscale", "status", stdout=_status_json()))

    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert captured_serve["trusted_hosts"] == ["console.example.com", TAILNET_NAME]


def test_no_env_var_is_needed_for_the_host_to_be_trusted(
    fake_tailscale, captured_serve, monkeypatch, tmp_path: Path
) -> None:
    """The resolved name is passed directly — not via CR_TRUSTED_HOSTS."""
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake_tailscale(status=_completed("tailscale", "status", stdout=_status_json()))
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


def test_cli_reports_a_refused_serve_but_still_starts_the_console(
    fake_tailscale, captured_serve, monkeypatch
) -> None:
    """The flag is a convenience: a refusal is a warning, not a dead console."""
    monkeypatch.delenv("CR_TRUSTED_HOSTS", raising=False)
    fake_tailscale(
        status=_completed("tailscale", "status", stdout=_status_json()),
        children=[_FakeProcess(exit_code=1, stderr="serve: access denied")],
    )
    result = CliRunner().invoke(_web_command(), ["--tailscale", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert "serve: access denied" in result.output
    assert "starting anyway" in result.output
    assert "--tailscale-port" in result.output
    assert "Traceback" not in result.output
    assert captured_serve["trusted_hosts"] == [TAILNET_NAME]  # console did start


def test_help_states_the_security_shape_and_the_port_flag() -> None:
    result = CliRunner().invoke(_web_command(), ["--help"])
    assert result.exit_code == 0
    assert "--tailscale" in result.output
    assert "--tailscale-port" in result.output
    assert "tailnet is the authentication" in result.output
