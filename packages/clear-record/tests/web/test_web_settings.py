"""The Settings page: one section per URL, read-mostly, and one agent panel.

Settings is the control plane (ADR-0027). Every section is its own real URL
with the nav marked, an unknown section is a page 404, and the only writes are
the two the console already had (the agent endpoint and the MCP client config).
The read sections show the value and the config path; they never carry a write.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from clear_record.service import Registry, paths
from clear_record.service.setup import MCP_SERVER_NAME
from clear_record.web.app import SETTINGS_SECTIONS, create_app

#: The slugs the pinned section list must expose, in order.
SECTIONS = tuple(slug for slug, _ in SETTINGS_SECTIONS)

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
