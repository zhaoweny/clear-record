"""The Settings page: one section per URL, read-mostly, and one agent panel.

Settings is the control plane (ADR-0027). Every section is its own real URL
with the nav marked, and an unknown section is a page 404. Most sections only
read; the writes are the agent setup (the harness path and the MCP client
config), the Models download (a user-triggered, verified checkpoint fetch) and
Status's walk-setup-again (which forgets the setup marker and nothing else).
"""

from __future__ import annotations

import json
import re

from _console import signed_in
from clear_record.core import paths
from clear_record.service import Registry, managed
from clear_record.service.setup import MCP_SERVER_NAME
from clear_record.web import app as web_app
from clear_record.web.app import LANG_COOKIE, SETTINGS_SECTIONS, create_app
from fastapi.testclient import TestClient

#: The slugs the pinned section list must expose, in order.
SECTIONS = tuple(slug for slug, _label, _template in SETTINGS_SECTIONS)

#: The agent-setup write surfaces, and the routes only they post to.
WRITE_ROUTES = (
    "/web/ui/agent-setup/mcp/harness",
    "/web/ui/agent-setup/mcp/config",
)


def _client(tmp_path) -> TestClient:
    return signed_in(
        TestClient(
            create_app(
                Registry.open(db_path=tmp_path / "registry.sqlite3"),
                trusted_hosts=("testserver",),
            )
        )
    )


def test_settings_lands_on_the_first_section(tmp_path) -> None:
    page = _client(tmp_path).get("/web/settings")

    assert page.status_code == 200
    assert SECTIONS[0] == "models"
    assert "<h2>Models</h2>" in page.text
    # The section nav is real links with the first marked; no dashboard.
    assert 'href="/web/settings/models" aria-current="page"' in page.text
    for slug in SECTIONS:
        assert f'href="/web/settings/{slug}"' in page.text


def test_every_section_is_a_real_url_with_the_nav_marked(tmp_path) -> None:
    client = _client(tmp_path)

    for slug in SECTIONS:
        page = client.get(f"/web/settings/{slug}")
        assert page.status_code == 200, slug
        assert 'id="detail"' in page.text
        assert f'href="/web/settings/{slug}" aria-current="page"' in page.text


def test_an_unknown_section_is_a_page_not_json(tmp_path) -> None:
    missing = _client(tmp_path).get("/web/settings/does-not-exist")

    assert missing.status_code == 404
    assert "text/html" in missing.headers["content-type"]
    assert "Not found" in missing.text


def test_the_agent_setup_writes_stay_out_of_the_read_sections(tmp_path) -> None:
    """Read-mostly: the agent-setup writes are mounted only where the agent panel is.

    The other sections do write — Models downloads a checkpoint, Status walks the
    setup again and re-runs the hello-world check (the module docstring above lists
    them) — so what this pins is narrower and checkable: the four agent-setup POSTs
    are reachable from the agent panel's own section, not from a page that only
    shows a value.
    """
    client = _client(tmp_path)

    for slug in ("models", "backends", "storage", "status"):
        page = client.get(f"/web/settings/{slug}")
        assert page.status_code == 200, slug
        for route in WRITE_ROUTES:
            assert route not in page.text, (slug, route)


def test_the_models_section_shows_its_values_and_the_config_path(tmp_path) -> None:
    page = _client(tmp_path).get("/web/settings/models")

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

    page = _client(tmp_path).get("/web/settings/models")

    assert "<code>small</code>" in page.text
    assert "ggml-small.bin" not in page.text


def test_the_models_section_offers_the_picker_and_a_download(
    tmp_path, monkeypatch
) -> None:
    """The one write on this page: a user-triggered, verified model download."""
    monkeypatch.setattr(web_app, "models_on_disk", lambda *a, **k: frozenset())

    page = _client(tmp_path).get("/web/settings/models")

    assert "Available checkpoints" in page.text
    for name in ("tiny", "base", "small", "medium", "large-v3"):
        assert f"<code>{name}</code>" in page.text
    assert 'hx-post="/web/ui/settings/models/download"' in page.text


def test_the_models_section_describes_checkpoints_truthfully(
    tmp_path, monkeypatch
) -> None:
    """A download adds a checkpoint; it does not persist a selection.

    --auto picks the largest suitable checkpoint already on disk, and a
    specific size is forced per run, so the old "downloading is how you
    switch" copy was false and must not come back.
    """
    monkeypatch.setattr(web_app, "models_on_disk", lambda *a, **k: frozenset())

    page = _client(tmp_path).get("/web/settings/models").text

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
        "/web/ui/settings/models/download", data={"model": "small"}
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
    page = client.get("/web/settings/models").text

    assert "可用检查点" in page
    assert "下载某个规格会把对应检查点放到磁盘上" in page
    assert "Available checkpoints" not in page


#: The retention note, translated — pinned whole, never in fragments: a catalog
#: that carries a second copy of an older translation (the way a hand-merged
#: ``msgstr`` continuation does) still contains every fragment, and the console
#: then renders the note twice.
RETENTION_NOTE_ZH = (
    "保留仅限手动：clear-record 绝不会自行删除录音。"
    "删除录音需要会议拥有已验证的归档作为持久副本，而位于用户选择的工作区中的录音一律拒绝删除。"
    "在 {path} 中设置托管根目录；项目的归档根目录在项目页中编辑。"
)


def test_the_retention_note_is_translated_in_a_chinese_console(tmp_path) -> None:
    """The retention note ships translated, **once**, and says what it now says.

    It names both refusals a tape delete can meet — no verified archive, and a
    user-chosen workspace (which is refused however archived) — and the paragraph
    it renders is compared as a whole, so a doubled or stale ``msgstr`` fails.
    """
    client = _client(tmp_path)
    client.cookies.set(LANG_COOKIE, "zh_CN")
    page = client.get("/web/settings/storage").text

    notes = re.findall(r'<p class="muted settings-note">(.*?)</p>', page, re.S)
    (note,) = [item for item in notes if "保留仅限手动" in item]
    assert note == RETENTION_NOTE_ZH.format(path=paths.config_path())
    assert "Retention is manual only" not in page


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
        "/web/ui/settings/models/download", data={"model": "medium"}
    )

    assert response.status_code == 200
    assert seen == ["medium"]
    assert "<code>medium</code>" in response.text
    assert "on disk" in response.text


def test_the_backends_section_lists_every_catalog_backend(tmp_path) -> None:
    page = _client(tmp_path).get("/web/settings/backends")

    # The four shipped families, in the service's catalog order.
    for backend_id in ("apple", "nvidia", "amd", "apple-speech"):
        assert f">{backend_id}<" in page.text
    # Read-only, so it names the config file a user would edit.
    assert str(paths.config_path()) in page.text


def test_the_webhooks_section_shows_the_panel_and_the_config_path(tmp_path) -> None:
    page = _client(tmp_path).get("/web/settings/webhooks")

    assert 'id="webhooks"' in page.text
    assert 'hx-get="/web/ui/webhooks"' in page.text
    # The panel itself is the write-free status surface; the page names where
    # endpoints are edited.
    assert str(paths.config_path()) in page.text


def test_the_storage_section_shows_the_managed_root_and_archive_roots(tmp_path) -> None:
    page = _client(tmp_path).get("/web/settings/storage")

    assert "Managed root" in page.text
    assert "Free space" in page.text
    assert "Archive roots" in page.text
    assert "Retention is manual only" in page.text
    assert str(paths.resolve_workspace_root()) in page.text


def test_the_storage_section_shows_the_machine_total_per_project(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project(
        "Ops",
        actor="console",
    )
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        actor="console",
    )
    meeting = managed.ensure_managed_workspace(registry, meeting, actor="console")
    client = signed_in(TestClient(create_app(registry, trusted_hosts=("testserver",))))

    page = client.get("/web/settings/storage").text

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
    assert "Kickoff" in page
    assert "managed by clear-record" in page


def test_the_storage_section_renders_a_partial_total_as_a_lower_bound(
    tmp_path, monkeypatch
) -> None:
    """A bucket the service could not measure is named, and the total says >=."""
    from clear_record.service import managed as managed_service

    def partial(registry, root=None):
        return {
            "buckets": [
                {
                    "id": "chunks",
                    "label": "Chunk cache",
                    "kind": "derived",
                    "bytes": 0,
                    "size": "0 B",
                    "partial": True,
                }
            ],
            "total_bytes": 20,
            "total_size": "20 B",
            "unknown": ["chunks"],
            "partial": True,
            "projects": [],
        }

    monkeypatch.setattr(managed_service, "machine_storage", partial)
    page = _client(tmp_path).get("/web/settings/storage")

    assert "&gt;= 20 B" in page.text
    assert "at least: some components could not be measured" in page.text
    assert "Chunk cache" in page.text


def test_the_status_section_carries_diagnostics_and_the_hello_check(tmp_path) -> None:
    page = _client(tmp_path).get("/web/settings/status")

    assert "Version" in page.text
    assert "Queue" in page.text
    assert "Backend availability" in page.text
    assert str(paths.resolve_data_dir()) in page.text
    assert str(paths.resolve_logs_dir()) in page.text
    assert 'href="/web/ui/diagnostics"' in page.text
    assert 'id="hello-check"' in page.text


def test_a_run_another_writer_started_appears_in_the_status_queue(tmp_path) -> None:
    """The console lists the registry's queue, not one manager's memory.

    The MCP server is another writer with its own manager over the same registry;
    the row it enqueues is what the console's Status section shows, and the row
    records the origin the status data can report. The run here is executed by
    the console's own manager, which is how queued work from any writer drains.
    """
    import dataclasses
    import threading
    import time

    from clear_record.service import PipelineOptions, RunManager

    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    release = threading.Event()
    manager = RunManager(registry, pipeline=lambda *args: release.wait(10))
    client = signed_in(
        TestClient(create_app(registry, runs=manager, trusted_hosts=("testserver",)))
    )

    registry.create_project(
        "Ops",
        actor="console",
    )
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(tmp_path),
        actor="console",
    )
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    registry.set_recording_set(
        meeting.id,
        [str(tape)],
        actor="console",
    )
    run = registry.create_run(
        meeting.id,
        backend="apple",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
        origin="mcp",
        actor="console",
    )
    try:
        for _ in range(1000):
            if registry.get_run(run.id).status == "running":
                break
            time.sleep(0.005)
        page = client.get("/web/settings/status")
        assert registry.get_run(run.id).origin == "mcp"
        assert page.status_code == 200
        assert "Kickoff" in page.text  # the queue panel lists the other writer's run
    finally:
        release.set()
        manager.shutdown(timeout=5)


def test_the_agent_flow_has_one_panel_and_two_entry_points(tmp_path) -> None:
    client = _client(tmp_path)

    settings = client.get("/web/settings/agent").text
    setup_page = client.get("/web/setup/agent").text

    for page in (settings, setup_page):
        assert 'id="agent-setup"' in page
        assert 'hx-get="/web/ui/agent-setup"' in page


def test_the_section_nav_does_not_hide_agent_or_webhooks_in_the_header(
    tmp_path,
) -> None:
    """The header is structural (Projects/Settings/Setup), not a junk drawer."""
    page = _client(tmp_path).get("/web/settings/agent")

    header = page.text.split("<main>", 1)[0]
    assert "/web/ui/agent-setup" not in header
    assert "/web/ui/webhooks" not in header


def test_the_mcp_section_mounts_the_shared_mcp_partial(tmp_path) -> None:
    page = _client(tmp_path).get("/web/settings/mcp")

    assert 'id="mcp-setup"' in page.text
    assert 'hx-get="/web/ui/agent-setup?part=mcp"' in page.text


def test_the_mcp_fragment_is_the_mcp_rung_alone(tmp_path) -> None:
    client = _client(tmp_path)

    mcp = client.get("/web/ui/agent-setup?part=mcp").text
    agent = client.get("/web/ui/agent-setup").text

    assert "/web/ui/agent-setup/mcp/harness" in mcp
    assert "/web/ui/agent-setup/mcp/config" in mcp
    # The Try it stage belongs to the agent panel, not the MCP section.
    assert 'id="hello-check"' not in mcp
    assert 'id="hello-check"' in agent
    assert "/web/ui/agent-setup/mcp/config" in agent


def test_the_mcp_write_re_renders_only_the_mcp_rung(tmp_path) -> None:
    client = _client(tmp_path)
    chosen = tmp_path / "client" / "mcp.json"

    response = client.post(
        "/web/ui/agent-setup/mcp/config",
        data={"config": str(chosen), "part": "mcp"},
    )

    assert response.status_code == 200
    assert "Registered the clear-record MCP server" in response.text
    assert "/web/ui/agent-setup/mcp/config" in response.text
    assert 'id="agent-setup"' not in response.text
    document = json.loads(chosen.read_text(encoding="utf-8"))
    assert document["mcpServers"][MCP_SERVER_NAME] == {
        "command": "clear-record",
        "args": ["mcp"],
    }


def test_an_unavailable_backend_reason_is_translated_in_a_chinese_console(
    tmp_path, monkeypatch
) -> None:
    """The reason is a message node, so the console can translate it.

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
    page = client.get("/web/settings/backends")

    assert page.status_code == 200
    assert "需要 macOS 26+（当前为 Linux）" in page.text
    assert "requires macOS" not in page.text
