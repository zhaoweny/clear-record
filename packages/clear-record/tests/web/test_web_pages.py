"""The console's full pages: real URLs, a real nav, and page 404s.

Every page extends ``base.html`` (so the header nav is on each one), the URL
selects the project, and an unknown URL under a page route is a *page* 404 -
distinct from the fragment 404s htmx must not swap.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

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
