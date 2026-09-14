"""The console offers the diagnostics bundle as a download (no build step)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from clear_record.service import Registry
from clear_record.web.app import create_app


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CR_LOG_DIR", str(tmp_path / "logs"))
    app = create_app(Registry.open(db_path=tmp_path / "registry.sqlite3"))
    return TestClient(app)


def test_console_offers_the_bundle_as_a_download(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    res = client.get("/ui/diagnostics")

    assert res.status_code == 200
    disposition = res.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "clear-record-diagnostics.txt" in disposition
    assert "NOT TELEMETRY" in res.text
    assert "# backend availability" in res.text
    assert "# withheld" in res.text


def test_index_links_the_download(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert 'href="/ui/diagnostics"' in client.get("/").text
