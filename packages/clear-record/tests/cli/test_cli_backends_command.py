"""``clear-record backends`` reports availability *and the reason* (ADR-0019).

The catalog has no system backend registered yet, so these tests inject a fake
one to pin the seam the Apple/Windows backends will plug into: ``--all`` names
an unavailable backend's reason, and the default listing hides unavailable ones.
"""

from __future__ import annotations

import argparse

from clear_record.cli import cli
from clear_record.providers import Availability, BackendBase, BackendInfo


class _FakeSystemBackend(BackendBase):
    def __init__(self) -> None:
        self.info = BackendInfo(
            id="apple-speech",
            vendor="Apple",
            frameworks=("Speech",),
            description="fake system backend",
            default_model="system",
            runtime="system",
            parallelizable=False,
            chunked=False,
        )

    def availability(self) -> Availability:
        return Availability(False, "requires macOS 26+ (this is 15.0)")

    def transcribe(self, audio_path: str, **kwargs):  # pragma: no cover - unused
        raise AssertionError


def test_backends_all_reports_the_unavailable_reason(monkeypatch, capsys) -> None:
    monkeypatch.setitem(cli.BACKENDS, "apple-speech", _FakeSystemBackend())

    assert cli._main(argparse.Namespace(command="backends", all=True)) == 0

    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("apple-speech"))
    assert "unavailable" in line
    assert "macOS 26+" in line


def test_backends_hides_unavailable_without_all(monkeypatch, capsys) -> None:
    monkeypatch.setitem(cli.BACKENDS, "apple-speech", _FakeSystemBackend())

    cli._main(argparse.Namespace(command="backends", all=False))

    assert "apple-speech" not in capsys.readouterr().out
