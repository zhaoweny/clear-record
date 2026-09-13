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
# Discovered by glob, so a new workspace member needs no edit here (ADR-0011).
MEMBER_PYPROJECTS = sorted(REPO_ROOT.glob("packages/*/pyproject.toml"))
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
# The virtual root plus every member: all move together (ADR-0011).
ALL_PYPROJECTS = [ROOT_PYPROJECT, *MEMBER_PYPROJECTS]
PACKAGE_DIRS = [path.parent for path in MEMBER_PYPROJECTS]

# The publishable shapes: X.Y.Z or X.Y.Z.devN (no local versions; PyPI rejects
# them — ADR-0011).
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\.dev\d+)?$")

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


def test_all_manifests_share_one_pep440_version() -> None:
    """The virtual root and every member declare one PEP 440 version.

    The root is published nowhere but still carries the version, and the
    release-train bump must move all the literals together (ADR-0011), so a
    drift here is a release-train bug."""
    by_path = {
        p.relative_to(REPO_ROOT): _load(p)["project"]["version"] for p in ALL_PYPROJECTS
    }
    unique = set(by_path.values())
    assert len(unique) == 1, f"manifests diverge: {by_path}"
    version = unique.pop()
    assert _VERSION_RE.match(version), f"not X.Y.Z[.devN]: {version!r}"


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


def test_every_package_ships_the_root_license() -> None:
    """Each publishable package bundles a byte-identical copy of the root MIT
    `LICENSE` (ADR-0002).

    MIT requires the notice to accompany copies, and `license-files =
    ["LICENSE"]` only ships a file that exists — so a missing or drifted copy
    silently drops the license from a wheel/sdist."""
    root_license = (REPO_ROOT / "LICENSE").read_bytes()
    missing = [
        str(p.relative_to(REPO_ROOT))
        for p in PACKAGE_DIRS
        if not (p / "LICENSE").exists()
    ]
    assert not missing, f"packages missing a LICENSE: {missing}"
    drifted = [
        str((p / "LICENSE").relative_to(REPO_ROOT))
        for p in PACKAGE_DIRS
        if (p / "LICENSE").read_bytes() != root_license
    ]
    assert not drifted, f"LICENSE copies differ from the root LICENSE: {drifted}"
