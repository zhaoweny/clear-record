"""The Tailscale binary: resolve symlinks, honour ``CR_TAILSCALE``, name aborts.

``tailscale status --json`` can fail *before* it answers. On macOS the App Store
app bundle aborts when its CLI is invoked through the ``~/.local/bin/tailscale``
symlink (``BundleIdentifiers.swift``), and the old error called that "not logged
in". These tests pin the two fixes: the discovered binary is **resolved to its
real path** before it reaches ``subprocess``, ``CR_TAILSCALE`` overrides ``PATH``,
and a process that dies without usable JSON is diagnosed as an **abort** — never
as a login state.

No real Tailscale runs: ``subprocess.run`` is replaced with a recorder, and
discovery is driven by a hermetic ``PATH`` / ``CR_TAILSCALE`` under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clear_record.web import tailscale

TAILNET_NAME = "myhost.tailnet.ts.net"

#: The macOS failure verbatim, as it reaches us on stderr.
_BUNDLE_ABORT = (
    "Tailscale/BundleIdentifiers.swift:47: Fatal error: The current "
    "bundleIdentifier is unknown to the registry"
)


class _Runner:
    """A recording stand-in for ``subprocess.run`` that always returns ``result``."""

    def __init__(self, result: subprocess.CompletedProcess) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        return self.result


def _status(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["tailscale", "status", "--json"], returncode, stdout, stderr
    )


def _valid_status() -> str:
    return json.dumps(
        {"BackendState": "Running", "Self": {"DNSName": f"{TAILNET_NAME}."}}
    )


@pytest.fixture()
def clean_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient Tailscale: an empty ``PATH`` and no ``CR_TAILSCALE``."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("CR_TAILSCALE", raising=False)


def _fake_binary(directory: Path, name: str) -> Path:
    """A real, executable file standing in for the CLI."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


def _symlink_to(real: Path, link_dir: Path) -> Path:
    link_dir.mkdir(parents=True, exist_ok=True)
    link = link_dir / "tailscale"
    link.symlink_to(real)
    return link


# --- discovery: resolve the symlink, and let CR_TAILSCALE win ---------------- #
def test_resolve_follows_a_symlinked_cli_to_its_real_path(
    clean_discovery, tmp_path: Path, monkeypatch
) -> None:
    """The App-Store shape: PATH holds a symlink into the app bundle."""
    real = _fake_binary(tmp_path / "app" / "Contents" / "MacOS", "Tailscale")
    _symlink_to(real, tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert tailscale.resolve_tailscale_bin() == str(real.resolve())


def test_resolve_prefers_cr_tailscale_over_path(
    clean_discovery, tmp_path: Path, monkeypatch
) -> None:
    on_path = _fake_binary(tmp_path / "bin", "tailscale")
    override = _fake_binary(tmp_path / "custom", "my-tailscale")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("CR_TAILSCALE", str(override))

    resolved = tailscale.resolve_tailscale_bin()

    assert resolved == str(override.resolve())
    assert resolved != str(on_path.resolve())


def test_resolve_is_idempotent_after_resolution(
    clean_discovery, tmp_path: Path, monkeypatch
) -> None:
    """A second pass over the already-real path changes nothing."""
    real = _fake_binary(tmp_path / "app", "Tailscale")
    _symlink_to(real, tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    once = tailscale.resolve_tailscale_bin()
    monkeypatch.setenv("CR_TAILSCALE", once)
    assert tailscale.resolve_tailscale_bin() == once


def test_resolve_falls_back_to_the_bare_name_when_absent(clean_discovery) -> None:
    assert tailscale.resolve_tailscale_bin() == "tailscale"


def test_resolve_dns_name_invokes_the_resolved_real_path(
    clean_discovery, tmp_path: Path, monkeypatch
) -> None:
    real = _fake_binary(tmp_path / "app" / "Contents" / "MacOS", "Tailscale")
    _symlink_to(real, tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    runner = _Runner(_status(stdout=_valid_status()))
    monkeypatch.setattr(tailscale.subprocess, "run", runner)

    assert tailscale.resolve_dns_name() == TAILNET_NAME
    # The argv we actually hand to subprocess: the real path, not the symlink.
    assert runner.calls == [[str(real.resolve()), "status", "--json"]]


def test_resolve_dns_name_invokes_cr_tailscale_over_path(
    clean_discovery, tmp_path: Path, monkeypatch
) -> None:
    on_path = _fake_binary(tmp_path / "bin", "tailscale")
    override = _fake_binary(tmp_path / "custom", "tailscale")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("CR_TAILSCALE", str(override))
    runner = _Runner(_status(stdout=_valid_status()))
    monkeypatch.setattr(tailscale.subprocess, "run", runner)

    assert tailscale.resolve_dns_name() == TAILNET_NAME
    assert runner.calls == [[str(override.resolve()), "status", "--json"]]


# --- an abort is an abort, not a login state --------------------------------- #
def test_an_abort_without_json_is_diagnosed_as_an_abort(
    clean_discovery, monkeypatch
) -> None:
    runner = _Runner(_status(returncode=1, stderr=_BUNDLE_ABORT))
    monkeypatch.setattr(tailscale.subprocess, "run", runner)

    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    message = str(excinfo.value)

    # Its own words are surfaced, and the cause is named as an abort.
    assert "Fatal error" in message
    assert "abort" in message
    assert "app bundle" in message
    assert "symlink" in message
    assert "CR_TAILSCALE" in message
    # ... and the old login mis-diagnosis is gone.
    assert "logged in" not in message
    assert "sign in" not in message
    assert "tailscale up" not in message


def test_a_signal_death_is_an_abort_even_with_no_output(
    clean_discovery, monkeypatch
) -> None:
    """A Swift trap reports as a signal death; silence is still an abort."""
    runner = _Runner(_status(returncode=-4))  # SIGILL
    monkeypatch.setattr(tailscale.subprocess, "run", runner)

    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    message = str(excinfo.value)

    assert "abort" in message
    assert "CR_TAILSCALE" in message
    assert "logged in" not in message


def test_a_nonzero_exit_with_no_output_is_an_abort(
    clean_discovery, monkeypatch
) -> None:
    runner = _Runner(_status(returncode=3))
    monkeypatch.setattr(tailscale.subprocess, "run", runner)

    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    assert "abort" in str(excinfo.value)


def test_a_refusal_keeps_its_words_and_does_not_blame_the_app_bundle(
    clean_discovery, monkeypatch
) -> None:
    """The daemon-down message is Tailscale speaking, not a crash."""
    runner = _Runner(_status(returncode=1, stderr="The Tailscale GUI is not running."))
    monkeypatch.setattr(tailscale.subprocess, "run", runner)

    with pytest.raises(tailscale.TailscaleError) as excinfo:
        tailscale.resolve_dns_name()
    message = str(excinfo.value)

    assert "The Tailscale GUI is not running." in message
    assert "tailscale up" in message
    assert "app bundle" not in message
    assert "BundleIdentifiers" not in message
