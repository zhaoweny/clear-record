"""The Settings page: one section per URL, read-mostly, and one agent panel.

Settings is the control plane (ADR-0027). Every section is its own real URL
with the nav marked, and an unknown section is a page 404. Most sections only
read; the writes are the agent endpoint and the MCP client config, the Models
download (a user-triggered, verified checkpoint fetch) and Status's
walk-setup-again (which forgets the setup marker and nothing else).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from clear_record.service import Registry, managed, paths
from clear_record.service.setup import MCP_SERVER_NAME
from clear_record.web import app as web_app
from clear_record.web.app import LANG_COOKIE, SETTINGS_SECTIONS, create_app

#: The slugs the pinned section list must expose, in order.
SECTIONS = tuple(slug for slug, _label, _template in SETTINGS_SECTIONS)

#: The two write surfaces, and the routes only they post to.
WRITE_ROUTES = (
    "/ui/agent-setup/use",
    "/ui/agent-setup/pull",
    "/ui/agent-setup/mcp/harness",
    "/ui/agent-setup/mcp/config",
)


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            Registry.open(db_path=tmp_path / "registry.sqlite3"),
            trusted_hosts=("testserver",),
        )
    )


def test_settings_lands_on_the_first_section(tmp_path) -> None:
    page = _client(tmp_path).get("/settings")

    assert page.status_code == 200
    assert SECTIONS[0] == "models"
    assert "<h2>Models</h2>" in page.text
    # The section nav is real links with the first marked; no dashboard.
    assert 'href="/settings/models" aria-current="page"' in page.text
    for slug in SECTIONS:
        assert f'href="/settings/{slug}"' in page.text


def test_every_section_is_a_real_url_with_the_nav_marked(tmp_path) -> None:
    client = _client(tmp_path)

    for slug in SECTIONS:
        page = client.get(f"/settings/{slug}")
        assert page.status_code == 200, slug
        assert 'id="detail"' in page.text
        assert f'href="/settings/{slug}" aria-current="page"' in page.text


def test_an_unknown_section_is_a_page_not_json(tmp_path) -> None:
    missing = _client(tmp_path).get("/settings/does-not-exist")

    assert missing.status_code == 404
    assert "text/html" in missing.headers["content-type"]
    assert "Not found" in missing.text


def test_the_read_sections_never_carry_a_write_route(tmp_path) -> None:
    """Read-mostly: only agent and MCP mount a writing surface."""
    client = _client(tmp_path)

    for slug in ("models", "backends", "storage", "status"):
        page = client.get(f"/settings/{slug}")
        assert page.status_code == 200, slug
        for route in WRITE_ROUTES:
            assert route not in page.text, (slug, route)


def test_the_models_section_shows_its_values_and_the_config_path(tmp_path) -> None:
    page = _client(tmp_path).get("/settings/models")

    assert "Default model" in page.text
    assert "Default language" in page.text
    assert "Built-in profiles" in page.text
    # The resolved value, not a restatement: the service's own resolver.
    assert str(paths.resolve_models_dir()) in page.text
    assert str(paths.config_path()) in page.text


def test_the_models_section_lists_the_checkpoints_on_disk(tmp_path) -> None:
    """The list is the service's normalized size names, not the raw files."""
    models = paths.resolve_models_dir()
    models.mkdir(parents=True, exist_ok=True)
    (models / "ggml-small.bin").write_bytes(b"x")

    page = _client(tmp_path).get("/settings/models")

    assert "<code>small</code>" in page.text
    assert "ggml-small.bin" not in page.text


def test_the_models_section_offers_the_picker_and_a_download(
    tmp_path, monkeypatch
) -> None:
    """The one write on this page: a user-triggered, verified model download."""
    monkeypatch.setattr(web_app, "models_on_disk", lambda *a, **k: frozenset())

    page = _client(tmp_path).get("/settings/models")

    assert "Available checkpoints" in page.text
    for name in ("tiny", "base", "small", "medium", "large-v3"):
        assert f"<code>{name}</code>" in page.text
    assert 'hx-post="/ui/settings/models/download"' in page.text


def test_the_models_section_describes_checkpoints_truthfully(
    tmp_path, monkeypatch
) -> None:
    """A download adds a checkpoint; it does not persist a selection.

    --auto picks the largest suitable checkpoint already on disk, and a
    specific size is forced per run, so the old "downloading is how you
    switch" copy was false and must not come back.
    """
    monkeypatch.setattr(web_app, "models_on_disk", lambda *a, **k: frozenset())

    page = _client(tmp_path).get("/settings/models").text

    assert "Available checkpoints" in page
    assert "Choose a model" not in page
    assert "downloading a size is how you switch" not in page
    assert "--auto uses the largest checkpoint already on disk" in page
    assert "a specific size is forced per run" in page


def test_the_settings_download_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """The pinned fetch is synchronous and can run for minutes.

    The route must hand it to a worker thread rather than call it on the event
    loop, which would stall every other request (and the local console).
    """
    import threading

    seen: dict[str, threading.Thread] = {}

    def fake_download(model=None, **kwargs):
        seen["download"] = threading.current_thread()
        return ""

    def on_disk(*args, **kwargs):
        # models_context renders after the fetch and runs on the event loop,
        # so this names the loop thread for comparison.
        seen["event_loop"] = threading.current_thread()
        return frozenset()

    monkeypatch.setattr(web_app, "download_transcription_model", fake_download)
    monkeypatch.setattr(web_app, "models_on_disk", on_disk)

    response = _client(tmp_path).post(
        "/ui/settings/models/download", data={"model": "small"}
    )

    assert response.status_code == 200
    assert seen["download"] is not seen["event_loop"]


def test_the_models_checkpoint_copy_is_translated_in_a_chinese_console(
    tmp_path, monkeypatch
) -> None:
    """The new copy ships translated, not as an English fallback."""
    monkeypatch.setattr(web_app, "models_on_disk", lambda *a, **k: frozenset())

    client = _client(tmp_path)
    client.cookies.set(LANG_COOKIE, "zh_CN")
    page = client.get("/settings/models").text

    assert "可用检查点" in page
    assert "下载某个规格会把对应检查点放到磁盘上" in page
    assert "Available checkpoints" not in page


def test_downloading_a_model_from_settings_calls_the_pinned_downloader(
    tmp_path, monkeypatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(
        web_app,
        "download_transcription_model",
        lambda model=None, **kwargs: seen.append(model) or "",
    )
    monkeypatch.setattr(
        web_app, "models_on_disk", lambda *a, **k: frozenset({"medium"})
    )

    response = _client(tmp_path).post(
        "/ui/settings/models/download", data={"model": "medium"}
    )

    assert response.status_code == 200
    assert seen == ["medium"]
    assert "<code>medium</code>" in response.text
    assert "on disk" in response.text


def test_the_backends_section_lists_every_catalog_backend(tmp_path) -> None:
    page = _client(tmp_path).get("/settings/backends")

    # The four shipped families, in the service's catalog order.
    for backend_id in ("apple", "nvidia", "amd", "apple-speech"):
        assert f">{backend_id}<" in page.text
    # Read-only, so it names the config file a user would edit.
    assert str(paths.config_path()) in page.text


def test_the_webhooks_section_shows_the_panel_and_the_config_path(tmp_path) -> None:
    page = _client(tmp_path).get("/settings/webhooks")

    assert 'id="webhooks"' in page.text
    assert 'hx-get="/ui/webhooks"' in page.text
    # The panel itself is the write-free status surface; the page names where
    # endpoints are edited.
    assert str(paths.config_path()) in page.text


def test_the_storage_section_shows_the_managed_root_and_archive_roots(tmp_path) -> None:
    page = _client(tmp_path).get("/settings/storage")

    assert "Managed root" in page.text
    assert "Free space" in page.text
    assert "Archive roots" in page.text
    assert "Retention is manual only" in page.text
    assert str(paths.resolve_workspace_root()) in page.text

def test_the_storage_section_shows_the_machine_total_per_project(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    meeting = managed.ensure_managed_workspace(registry, meeting)
    client = TestClient(create_app(registry, trusted_hosts=("testserver",)))

    page = client.get("/settings/storage").text

    # The machine total and every component, each marked source or derived.
    assert "Machine total" in page
    assert "Chunk cache" in page
    assert "Model weights" in page
    assert "Source" in page
    assert "Derived" in page
    # The per-project breakdown names the workspace and whose disk it is on.
    assert "Per project" in page
    assert "Ops" in page
    assert str(meeting.workspace_path) in page
    assert "managed by clear-record" in page



def test_the_status_section_carries_diagnostics_and_the_hello_check(tmp_path) -> None:
    page = _client(tmp_path).get("/settings/status")

    assert "Version" in page.text
    assert "Queue" in page.text
    assert "Backend availability" in page.text
    assert str(paths.resolve_data_dir()) in page.text
    assert str(paths.resolve_logs_dir()) in page.text
    assert 'href="/ui/diagnostics"' in page.text
    assert 'id="hello-check"' in page.text


def test_the_agent_flow_has_one_panel_and_two_entry_points(tmp_path) -> None:
    client = _client(tmp_path)

    settings = client.get("/settings/agent").text
    setup_page = client.get("/setup/agent").text

    for page in (settings, setup_page):
        assert 'id="agent-setup"' in page
        assert 'hx-get="/ui/agent-setup"' in page


def test_the_section_nav_does_not_hide_agent_or_webhooks_in_the_header(
    tmp_path,
) -> None:
    """The header is structural (Projects/Settings/Setup), not a junk drawer."""
    page = _client(tmp_path).get("/settings/agent")

    header = page.text.split("<main>", 1)[0]
    assert "/ui/agent-setup" not in header
    assert "/ui/webhooks" not in header


def test_the_mcp_section_mounts_the_shared_mcp_partial(tmp_path) -> None:
    page = _client(tmp_path).get("/settings/mcp")

    assert 'id="mcp-setup"' in page.text
    assert 'hx-get="/ui/agent-setup?part=mcp"' in page.text


def test_the_mcp_fragment_is_the_mcp_rung_alone(tmp_path) -> None:
    client = _client(tmp_path)

    mcp = client.get("/ui/agent-setup?part=mcp").text
    agent = client.get("/ui/agent-setup").text

    assert "/ui/agent-setup/mcp/harness" in mcp
    assert "/ui/agent-setup/mcp/config" in mcp
    # The endpoint rung belongs to the agent panel, not the MCP section.
    assert "/ui/agent-setup/use" not in mcp
    assert "/ui/agent-setup/use" in agent
    assert "/ui/agent-setup/mcp/config" in agent


def test_the_mcp_write_re_renders_only_the_mcp_rung(tmp_path) -> None:
    client = _client(tmp_path)
    chosen = tmp_path / "client" / "mcp.json"

    response = client.post(
        "/ui/agent-setup/mcp/config",
        data={"config": str(chosen), "part": "mcp"},
    )

    assert response.status_code == 200
    assert "Registered the clear-record MCP server" in response.text
    assert "/ui/agent-setup/mcp/config" in response.text
    assert 'id="agent-setup"' not in response.text
    document = json.loads(chosen.read_text(encoding="utf-8"))
    assert document["mcpServers"][MCP_SERVER_NAME] == {
        "command": "clear-record",
        "args": ["mcp"],
    }


def test_an_unavailable_backend_reason_is_translated_in_a_chinese_console(
    tmp_path, monkeypatch
) -> None:
    """Ticket 08: the reason is a message node, so the console can translate it.

    The service hands backend_status a JSON message node (an ID plus
    parameters); the shared backend list renders it with the boundary's
    translation lookup, so a zh_CN console shows the translated reason rather
    than the raw English.
    """
    message_node = {
        "id": "requires macOS {major}+ (this is {host})",
        "params": {"major": 26, "host": "Linux"},
    }
    monkeypatch.setattr(
        "clear_record.web.app.backend_status",
        lambda: {"apple-speech": {"available": False, "reason": message_node}},
    )

    client = _client(tmp_path)
    client.cookies.set(LANG_COOKIE, "zh_CN")
    page = client.get("/settings/backends")

    assert page.status_code == 200
    assert "需要 macOS 26+（当前为 Linux）" in page.text
    assert "requires macOS" not in page.text
