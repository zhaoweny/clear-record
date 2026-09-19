"""Guard: the release and architecture docs stay true to the repo.

Each check pins a claim that had drifted in the v0.2.0 docs review: wording that
contradicted the remote, an ADR that labelled an unbuilt idea a decision, a stale
tool count, and a wrong relative link. A doc claim re-breaks silently, so the
narrow assertions live here. This is not a prose linter; extend it one named
claim at a time.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Every relative markdown link in the docs corpus must resolve to a real file.
_DOC_GLOBS = ("docs/**/*.md", "README.md", "CONTEXT.md", "SECURITY.md", "AGENTS.md")
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_TICK = "`"  # a backtick, written literally rather than via chr(96)
_FENCE = re.compile(_TICK * 3 + ".*?" + _TICK * 3, re.DOTALL)
_CODE = re.compile(_TICK + "[^" + _TICK + "]*" + _TICK)


def _text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_releasing_records_the_real_publish_state() -> None:
    """The remote carries both lines and the tags; v0.1.1 is on PyPI."""
    text = _text("docs/releasing.md")
    assert "nothing has been pushed" not in text
    assert "released tags are on" in text
    assert "live on PyPI" in text


def test_x_forwarded_is_an_open_not_a_decision() -> None:
    """ADR-0021 recorded a steer with no implementation, so it is [OPEN]."""
    adr = _text("docs/adr/0021-localhost-only-deployment.md")
    assert "[OPEN: owner, 2026-09-15]" in adr
    assert "deferred, not built" in adr
    assert "[DECISION: owner, 2026-09-15] **Honour" not in adr
    guide = _text("docs/service-deployment.md")
    assert "deferred, not built" in guide


def _tool_names() -> list[str]:
    source = _text("packages/clear-record/src/clear_record/mcp/server.py")
    block = source.split("TOOL_NAMES", 1)[1].split(")", 1)[0]
    return re.findall(r'"([a-z_]+)"', block)


def _adr_0017_surface() -> list[str]:
    """The tools listed in ADR-0017's Decision surface block."""
    adr = _text("docs/adr/0017-mcp-server.md")
    block = adr.split("The surface:", 1)[1].split("\n- [DECISION]", 1)[0]
    return re.findall(r"`([a-z_]+)`", block)


def test_adr_0017_documents_the_agent_task_tools() -> None:
    """The surface the ADR lists is exactly the surface the server registers."""
    adr = _text("docs/adr/0017-mcp-server.md")
    tools = _tool_names()
    for tool in (
        "list_agent_drafts",
        "read_agent_draft",
        "run_agent_task",
        "accept_agent_draft",
        "reject_agent_draft",
    ):
        assert tool in tools, f"{tool} is not registered in TOOL_NAMES"
    surface = _adr_0017_surface()
    drift = set(tools) ^ set(surface)
    assert not drift, f"ADR-0017's surface list drifted from TOOL_NAMES: {drift}"
    assert sorted(surface) == sorted(tools)
    assert "[OPEN] Agent-task tools" not in adr
    assert "landed" in adr


def test_adr_0018_tool_count_is_current() -> None:
    """'14 tools' described 2026-09-14; the surface is 22 today."""
    adr = _text("docs/adr/0018-agent-task-execution.md")
    assert "then 14 tools" in adr
    assert "22 tools" in adr
    assert len(_tool_names()) == 22


def test_issue_tracker_names_the_real_guard_classes() -> None:
    """The convention's enforcement claim matches what the guard actually runs.

    It used to name the constant ``IGNORED_PATH_ROOTS``. ADR-0029 added the
    private instance as a second class, so the doc now names the classes rather
    than the identifiers — the guard stays the source of truth, so both classes
    must still exist and be the ones the scan calls.
    """
    tracker = _text("docs/agents/issue-tracker.md")
    assert "path roots and the instance hostname" in tracker
    assert "packages/clear-record/tests/test_tracker_refs.py" in tracker
    assert "IGNORED_PREFIXES" not in tracker
    guard = _text("packages/clear-record/tests/test_tracker_refs.py")
    for name in ("IGNORED_PATH_ROOTS", "PRIVATE_HOSTS"):
        assert f"{name} = " in guard, f"the guard no longer defines {name}"
    assert "_references_root(" in guard
    assert "_references_host(" in guard


def test_adr_0027_sitemap_lists_the_project_subroutes() -> None:
    adr = _text("docs/adr/0027-console-information-architecture.md")
    app = _text("packages/clear-record/src/clear_record/web/app.py")
    for route in (
        "/projects/<slug>/meetings",
        "/projects/<slug>/glossary",
        "/projects/<slug>/media",
    ):
        assert route in adr, f"{route} missing from ADR-0027's sitemap"
        assert route.replace("<slug>", "{slug}") in app


def test_readme_documents_the_bind_and_the_layers() -> None:
    readme = _text("README.md")
    assert "server binds **localhost only**" not in readme
    source = _text("packages/clear-record/src/clear_record/web/__init__.py")
    default = re.search(r'DEFAULT_HOST = "([^"]+)"', source)
    assert default is not None, "web/__init__.py no longer defines DEFAULT_HOST"
    assert f"binds `{default.group(1)}` by default" in readme
    for layer in ("service", "tray", "mcp"):
        assert f"src/clear_record/{layer}" in readme, f"{layer} missing from the layout"


def test_adr_0013_records_the_later_base_dependencies() -> None:
    adr = _text("docs/adr/0013-bundled-web-and-service-surface.md")
    assert "Superseded in part by [ADR-0022](0022-adopt-click.md)" in adr
    for dep in ("click", "platformdirs", "json-repair"):
        assert dep in adr


def test_docs_relative_links_resolve() -> None:
    """Every relative markdown link in the corpus points at a real file."""
    problems: list[str] = []
    seen: set[Path] = set()
    for pattern in _DOC_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            text = _CODE.sub("", _FENCE.sub("", path.read_text(encoding="utf-8")))
            for match in _LINK.finditer(text):
                target = match.group(1).strip().split("#", 1)[0]
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                if not (path.parent / target).resolve().exists():
                    problems.append(
                        f"{path.relative_to(REPO_ROOT)} -> {match.group(1)}"
                    )
    assert not problems, "broken relative links:\n" + "\n".join(problems)
