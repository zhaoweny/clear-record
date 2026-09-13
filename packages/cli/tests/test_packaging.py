"""Packaging regression guard: the workspace dists release in lockstep.

The five workspace members ship one version and every intra-project (`cr-*`)
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
ALIAS_PYPROJECT = REPO_ROOT / "packages" / "clear-record" / "pyproject.toml"
MEMBER_PYPROJECTS = [
    REPO_ROOT / "packages" / "core" / "pyproject.toml",
    REPO_ROOT / "packages" / "engine" / "pyproject.toml",
    REPO_ROOT / "packages" / "providers" / "pyproject.toml",
    CLI_PYPROJECT,
    ALIAS_PYPROJECT,
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


def test_cli_declares_no_console_script() -> None:
    """`cr-cli` is the CLI implementation and declares no command (ADR-0009).

    The `clear-record` facade is the sole owner of the `clear-record` console
    script; a script here too would be a second owner of the command."""
    scripts = _load(CLI_PYPROJECT)["project"].get("scripts", {})
    assert not scripts, f"cr-cli must declare no console scripts: {scripts}"


def test_alias_declares_the_clear_record_script() -> None:
    """The `clear-record` alias dist owns the command itself (ADR-0009).

    `uvx clear-record` resolves the alias, so the alias must own the entry
    point; if it only depended on `cr-cli`, uv would warn that the command
    comes from a dependency. The alias points at its own facade module, which
    forwards to the implementation."""
    scripts = _load(ALIAS_PYPROJECT)["project"]["scripts"]
    assert scripts.get("clear-record") == "clear_record:main"
    assert "clearrecord" not in scripts


def test_alias_facade_forwards_to_the_implementation() -> None:
    """The alias facade actually forwards to `cr_cli.cli.main` (ADR-0009).

    The alias ships a small module so `uv_build` has something to build; the
    module must re-export the real entry point rather than a copy."""
    import clear_record
    from cr_cli import cli

    assert clear_record.main is cli.main
