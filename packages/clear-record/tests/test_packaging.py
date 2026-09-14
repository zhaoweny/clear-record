"""Packaging regression guard for the single `clear-record` distribution.

Since the collapse (ADR-0012) the workspace publishes **one** dist, so the old
five-member lockstep / exact-pin checks are gone. What remains: the virtual root
and the one member carry one PEP 440 version, no `cr-*` dependency survives
anywhere, the dist bundles a byte-identical copy of the root MIT `LICENSE`,
carries real PyPI metadata, and owns the `clear-record` command. Static checks
over pyproject.toml plus the packaged Trove classifier list — no network, no
build.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

# This file lives at packages/clear-record/tests/, so parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
MEMBER_PYPROJECTS = sorted(REPO_ROOT.glob("packages/*/pyproject.toml"))
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
# The virtual root plus the single member: the root's version is vestigial (it
# is never built or published), so it is kept in step with the member by a
# second `uv version` call in each `just` recipe. A drift here is a bump bug
# (ADR-0011's 2026-09-13 simplification update).
ALL_PYPROJECTS = [ROOT_PYPROJECT, *MEMBER_PYPROJECTS]
PACKAGE_DIRS = [path.parent for path in MEMBER_PYPROJECTS]

# The version shapes the manifests carry: stable X.Y.Z, a pre-release
# X.Y.Z{a|b|rc}N (a/b accepted but unused), and the in-development X.Y.Z.devN
# marker, which is a CI artifact and never published (no local versions; PyPI
# rejects them — ADR-0011).
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+((?:a|b|rc)\d+)?(?:\.dev\d+)?$")

# A PEP 508 dependency string starts with the distribution name; split it off.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+")

# Third-party runtime dependencies of the single dist. ADR-0013 kept this
# audio-only set: the web console's stack (fastapi/uvicorn) lives in the `web`
# extra, not the base install, so `clear-record` stays cheap for CLI-only users
# while the web *code* still ships in this one wheel.
RUNTIME_DEPS = {"numpy", "soundfile"}

# The optional `web` extra's dependency set (ADR-0013). It must not leak into
# the base dependencies; the MCP SDK gets its own extra when the MCP server lands.
WEB_EXTRA_DEPS = {"fastapi", "uvicorn", "jinja2", "python-multipart"}
TRAY_EXTRA_DEPS = {"pyside6"}


def _load(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _all_dependencies(project: dict) -> list[str]:
    deps = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        deps.extend(extra_deps)
    return deps


def test_all_manifests_share_one_pep440_version() -> None:
    """The virtual root and the single member declare one PEP 440 version.

    The root is published nowhere but still carries the version, and each
    `just` bump recipe runs `uv version` twice so both literals move together
    (ADR-0011's 2026-09-13 simplification update), so a drift here is a bump
    bug."""
    by_path = {
        p.relative_to(REPO_ROOT): _load(p)["project"]["version"] for p in ALL_PYPROJECTS
    }
    unique = set(by_path.values())
    assert len(unique) == 1, f"manifests diverge: {by_path}"
    version = unique.pop()
    assert _VERSION_RE.match(version), f"not X.Y.Z[{{a|b|rc}}N][.devN]: {version!r}"


def test_no_cr_dependency_survives_the_collapse() -> None:
    """No manifest anywhere depends on a `cr-*` dist.

    The four `cr-*` layers are now subpackages of the one dist (ADR-0012); a
    lingering `cr-core`/`cr-engine`/`cr-providers`/`cr-cli` dependency would be
    unresolvable on PyPI."""
    offenders: list[str] = []
    for path in ALL_PYPROJECTS:
        for spec in _all_dependencies(_load(path)["project"]):
            name = _NAME_RE.match(spec)
            if name and name.group().startswith("cr-"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {spec}")
    assert not offenders, "obsolete `cr-*` dependencies: " + ", ".join(offenders)


def test_single_dist_runtime_dependencies() -> None:
    """The one published dist depends only on numpy/soundfile (ADR-0012).

    This is the packaging face of the vendor-free boundary that now lives in
    `test_layering.py`: the dist metadata names no vendor stack and no
    intra-workspace pin."""
    assert len(MEMBER_PYPROJECTS) == 1, (
        "expected exactly one publishable package, found "
        f"{[p.relative_to(REPO_ROOT) for p in MEMBER_PYPROJECTS]}"
    )
    project = _load(MEMBER_PYPROJECTS[0])["project"]
    names = {_NAME_RE.match(spec).group() for spec in project.get("dependencies", [])}
    assert names == RUNTIME_DEPS, f"unexpected runtime dependencies: {sorted(names)}"


def test_web_surface_is_an_extra_not_a_base_dependency() -> None:
    """The web console's stack is gated behind the `web` extra (ADR-0013).

    The console ships inside this one wheel as `clear_record.web` (no second dist
    to publish), but its dependencies must not: a CLI-only install stays
    audio-only. `web` is still registered as a subcommand by an entry point, so
    it is discoverable without the CLI importing the web module.
    """
    project = _load(MEMBER_PYPROJECTS[0])["project"]
    extras = project.get("optional-dependencies", {})
    names = {
        extra: {_NAME_RE.match(spec).group() for spec in specs}
        for extra, specs in extras.items()
    }
    assert names.get("web") == WEB_EXTRA_DEPS, names.get("web")
    assert names.get("tray") == TRAY_EXTRA_DEPS, names.get("tray")

    base = {_NAME_RE.match(spec).group() for spec in project.get("dependencies", [])}
    leaked = (WEB_EXTRA_DEPS | TRAY_EXTRA_DEPS) & base
    assert not leaked, f"optional stack leaked into base dependencies: {sorted(leaked)}"

    declared = project.get("entry-points", {}).get("clear_record.commands", {})
    assert declared.get("web") == "clear_record.web:register", declared
    assert declared.get("tray") == "clear_record.tray:register", declared


def test_web_console_assets_ship_with_the_package() -> None:
    """The console's templates and vendored libraries are package data.

    They must exist on disk (the wheel ships them; the frozen build collects
    them), so a rename that orphans the UI fails here rather than at runtime.
    """
    web = PACKAGE_DIRS[0] / "src" / "clear_record" / "web"
    required = (
        "templates/base.html",
        "templates/index.html",
        "templates/_projects.html",
        "templates/_detail.html",
        "static/app.css",
        "static/htmx.min.js",
        "static/alpine.min.js",
    )
    missing = [rel for rel in required if not (web / rel).is_file()]
    assert not missing, f"missing web console assets: {missing}"


def test_declares_the_clear_record_script() -> None:
    """The single dist owns the `clear-record` command (ADR-0009, ADR-0012).

    The entry point targets the CLI layer directly, so `import clear_record`
    stays light; the old `clearrecord` spelling must not reappear."""
    scripts = _load(MEMBER_PYPROJECTS[0])["project"]["scripts"]
    assert scripts.get("clear-record") == "clear_record.cli:main"
    assert "clearrecord" not in scripts


def test_entry_point_resolves_to_the_cli_main() -> None:
    """The declared entry point actually reaches the CLI implementation."""
    from clear_record.cli import main
    from clear_record.cli.cli import main as cli_main

    assert main is cli_main


def test_import_clear_record_is_light() -> None:
    """Importing the top-level package must not pull in the heavy CLI stack.

    The console script targets `clear_record.cli:main` precisely so that a
    plain `import clear_record` does not import numpy/the CLI. Run in a fresh
    interpreter so pytest's own imports cannot mask a regression."""
    code = (
        "import sys, clear_record; "
        "heavy = [m for m in ('clear_record.cli', 'clear_record.engine', "
        "'clear_record.providers', 'numpy') if m in sys.modules]; "
        "assert not heavy, heavy"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_every_package_ships_the_root_license() -> None:
    """The publishable package bundles a byte-identical copy of the root MIT
    `LICENSE` (ADR-0002) and lists it in `license-files`.

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
    for path in MEMBER_PYPROJECTS:
        license_files = _load(path)["project"].get("license-files")
        assert license_files == ["LICENSE"], (
            f"{path.relative_to(REPO_ROOT)}: expected license-files = ['LICENSE'], "
            f"got {license_files!r}"
        )


def test_publishable_manifests_carry_pypi_metadata() -> None:
    """Every publishable dist declares authors, keywords and real classifiers.

    A manifest with none of these renders a PyPI page with no author and no
    classifiers, so the member must carry non-empty `authors`, `keywords` and
    `classifiers` (the virtual root is never published and is not checked here).
    Every classifier must exist in the canonical Trove list: PyPI rejects an
    unknown one at upload, so a typo would otherwise surface only after the
    artifacts are built. Static check over pyproject.toml plus the
    `trove-classifiers` dataset — no network, no build."""
    from trove_classifiers import classifiers as trove_classifiers

    problems: list[str] = []
    for path in MEMBER_PYPROJECTS:
        rel = path.relative_to(REPO_ROOT)
        project = _load(path)["project"]
        for field in ("authors", "keywords", "classifiers"):
            if not project.get(field):
                problems.append(f"{rel}: missing or empty {field!r}")
        problems.extend(
            f"{rel}: unknown classifier {c!r}"
            for c in project.get("classifiers", [])
            if c not in trove_classifiers
        )
    assert not problems, "incomplete PyPI metadata:\n" + "\n".join(problems)
