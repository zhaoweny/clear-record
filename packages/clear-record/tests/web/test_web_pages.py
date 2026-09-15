"""The console's full pages: real URLs, a real nav, and page 404s.

Every page extends ``base.html`` (so the header nav is on each one), the URL
selects the project, and an unknown URL under a page route is a *page* 404 -
distinct from the fragment 404s htmx must not swap.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from clear_record.core import RecordDocument, Segment, write_json
from clear_record.service import AgentConfig, Registry
from clear_record.web.app import create_app


def _client(tmp_path, **kwargs) -> TestClient:
    return TestClient(
        create_app(
            Registry.open(db_path=tmp_path / "registry.sqlite3"),
            trusted_hosts=("testserver",),
            **kwargs,
        )
    )


def _seeded(tmp_path):
    """A TestClient plus the registry it renders, for seeding real content."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = TestClient(create_app(registry, trusted_hosts=("testserver",)))
    return registry, client


def test_the_projects_page_carries_the_top_level_nav(tmp_path) -> None:
    client = _client(tmp_path, agent_config=AgentConfig())

    home = client.get("/")

    assert home.status_code == 200
    # Real links, plus hx-boost so a click is an in-place swap when JS is there.
    assert 'href="/"' in home.text
    assert 'href="/settings"' in home.text
    assert 'href="/setup"' in home.text
    assert 'hx-boost="true"' in home.text
    assert 'id="projects"' in home.text


def test_the_setup_link_is_hidden_once_an_agent_is_configured(tmp_path) -> None:
    ready = _client(tmp_path, agent_config=AgentConfig(endpoint="http://local.test/v1"))

    home = ready.get("/")

    assert 'href="/"' in home.text
    assert 'href="/settings"' in home.text
    assert 'href="/setup"' not in home.text


def test_settings_renders_a_section_and_unknown_sections_404(tmp_path) -> None:
    client = _client(tmp_path)

    assert client.get("/settings").status_code == 200
    agent = client.get("/settings/agent")
    assert agent.status_code == 200
    assert 'id="agent-setup"' in agent.text
    assert client.get("/settings/webhooks").status_code == 200

    missing = client.get("/settings/does-not-exist")
    assert missing.status_code == 404
    assert "text/html" in missing.headers["content-type"]


def test_setup_and_the_agent_step_render(tmp_path) -> None:
    client = _client(tmp_path)

    assert client.get("/setup").status_code == 200
    agent = client.get("/setup/agent")
    assert agent.status_code == 200
    assert 'id="agent-setup"' in agent.text


def test_a_project_has_its_own_page_and_url(tmp_path) -> None:
    client = _client(tmp_path)
    client.post("/api/projects", json={"name": "Weekly Ops"})

    page = client.get("/projects/weekly-ops")

    assert page.status_code == 200
    # The page renders the detail *and* keeps the in-page `#detail` swap target
    # the `/ui/*` project fragments render into.
    assert 'id="detail"' in page.text
    assert "Weekly Ops" in page.text
    assert 'aria-current="page"' in page.text


def test_an_unknown_project_404s_as_a_page(tmp_path) -> None:
    client = _client(tmp_path)

    missing = client.get("/projects/nope")

    assert missing.status_code == 404
    assert "text/html" in missing.headers["content-type"]
    assert "Not found" in missing.text


def test_the_page_shell_wraps_the_project_fragment(tmp_path) -> None:
    """The page owns the project; `/ui/projects/<slug>` still serves its body."""
    client = _client(tmp_path)
    client.post("/api/projects", json={"name": "Weekly Ops"})

    fragment = client.get("/ui/projects/weekly-ops")

    assert fragment.status_code == 200
    assert "Weekly Ops" in fragment.text
    # A fragment is a body, not a document: it must not carry the page shell.
    assert "<!doctype html>" not in fragment.text


def test_project_rows_label_their_counts(tmp_path) -> None:
    """The counts are legible at a glance, not bare numbers behind a title."""
    client = _client(tmp_path)
    client.post("/api/projects", json={"name": "Weekly Ops"})

    home = client.get("/")

    assert "0 meetings" in home.text
    assert "0 terms" in home.text


# --- the project page's sub-tabs (ticket 02) -------------------------------- #
def test_every_project_sub_tab_is_its_own_url(tmp_path) -> None:
    """Each tab is a real page with the active tab marked, not colour alone."""
    _registry, client = _seeded(tmp_path)
    client.post("/api/projects", json={"name": "Ops"})

    for path, active in (
        ("/projects/ops", "overview"),
        ("/projects/ops/meetings", "meetings"),
        ("/projects/ops/glossary", "glossary"),
        ("/projects/ops/media", "media"),
    ):
        page = client.get(path)
        assert page.status_code == 200
        assert 'id="detail"' in page.text
        assert 'aria-label="Project sections"' in page.text
        for tab, href in (
            ("overview", "/projects/ops"),
            ("meetings", "/projects/ops/meetings"),
            ("glossary", "/projects/ops/glossary"),
            ("media", "/projects/ops/media"),
        ):
            marker = ' aria-current="page"' if tab == active else ""
            assert f'href="{href}"{marker}' in page.text


def test_a_project_sub_tab_is_deep_linkable_and_refresh_safe(tmp_path) -> None:
    _registry, client = _seeded(tmp_path)
    client.post("/api/projects", json={"name": "Ops"})

    first = client.get("/projects/ops/media")
    second = client.get("/projects/ops/media")

    assert first.status_code == second.status_code == 200
    assert first.text == second.text


def test_the_meeting_review_is_a_page_under_the_project(tmp_path) -> None:
    registry, client = _seeded(tmp_path)
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))

    page = client.get("/projects/ops/meetings/kickoff")

    assert page.status_code == 200
    assert "Kickoff" in page.text
    # The project tabs stay, with Meetings marked.
    assert 'href="/projects/ops/meetings" aria-current="page"' in page.text

    missing = client.get("/projects/ops/meetings/does-not-exist")
    assert missing.status_code == 404
    assert "Not found" in missing.text


def test_the_media_tab_inventories_tapes_and_transcripts(tmp_path) -> None:
    """Media reuses the service's storage and transcript accounting."""
    registry, client = _seeded(tmp_path)
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_json(
        workspace / "record.json",
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(Segment(start=0.0, end=1.0, text="hello", source="mic"),),
        ),
    )
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    registry.register_tape(
        meeting.id, path=str(workspace / "a.wav"), sha256="a" * 64, bytes=2048
    )

    media = client.get("/ui/projects/ops/media")

    assert media.status_code == 200
    assert "a.wav" in media.text
    assert "2.0 KiB" in media.text
    assert ("a" * 12) in media.text  # the short sha256
    assert "record" in media.text  # the transcript's source
    assert "segments" in media.text
    assert 'href="/projects/ops/meetings/kickoff"' in media.text


def test_the_landing_shows_the_newest_meetings_across_projects(tmp_path) -> None:
    """One compact recent-activity line, from cheap registry reads only."""
    registry, client = _seeded(tmp_path)
    registry.create_project("Ops")
    registry.create_project("Field interviews")
    registry.create_meeting("ops", "Kickoff")
    registry.create_meeting("field-interviews", "Interview")

    home = client.get("/")

    assert "recent-activity" in home.text
    assert 'href="/projects/ops/meetings/kickoff"' in home.text
    assert 'href="/projects/field-interviews/meetings/interview"' in home.text


def test_the_landing_has_no_activity_line_without_meetings(tmp_path) -> None:
    _registry, client = _seeded(tmp_path)
    client.post("/api/projects", json={"name": "Ops"})

    assert "recent-activity" not in client.get("/").text
