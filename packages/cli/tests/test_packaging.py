"""Packaging regression guard: the workspace dists release in lockstep.

The four workspace members ship one version and every intra-project (`cr-*`)
dependency is exact-pinned, so a released set of wheels cannot resolve to mixed
versions. Static checks over pyproject.toml only — no network, no build.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# This file lives at packages/cli/tests/, so parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_PYPROJECT = REPO_ROOT / "packages" / "cli" / "pyproject.toml"
MEMBER_PYPROJECTS = [
    REPO_ROOT / "packages" / "core" / "pyproject.toml",
    REPO_ROOT / "packages" / "engine" / "pyproject.toml",
    REPO_ROOT / "packages" / "providers" / "pyproject.toml",
    CLI_PYPROJECT,
]
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"

# A PEP 508 dependency string starts with the distribution name; split it off.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+")


def _load(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _all_dependencies(project: dict) -> list[str]:
    deps = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        deps.extend(extra_deps)
    return deps


def test_member_versions_are_equal() -> None:
    versions = {_load(p)["project"]["version"] for p in MEMBER_PYPROJECTS}
    assert len(versions) == 1, f"member versions diverge: {sorted(versions)}"


def test_intra_project_dependencies_are_exact_pinned() -> None:
    """Every `cr-*` dep is pinned with `==` to the depending project's own
    version (version lockstep, ADR-0009). An unpinned or differently pinned
    sibling is reported with the pin that was expected."""
    offenders: list[str] = []
    for path in [*MEMBER_PYPROJECTS, ROOT_PYPROJECT]:
        project = _load(path)["project"]
        own_version = project["version"]
        for spec in _all_dependencies(project):
            name = _NAME_RE.match(spec)
            if not (name and name.group().startswith("cr-")):
                continue
            pinned = spec.partition("==")[2]
            if pinned != own_version:
                expected = f"{name.group()}=={own_version}"
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: {spec} (expected {expected})"
                )
    assert not offenders, "intra-project deps not version-locked: " + ", ".join(
        offenders
    )


def test_console_script_is_spelled_clear_record() -> None:
    """The installed command matches the owner's spelling (ADR-0009)."""
    scripts = _load(CLI_PYPROJECT)["project"]["scripts"]
    assert scripts.get("clear-record") == "cr_cli.cli:main"
    assert "clearrecord" not in scripts
