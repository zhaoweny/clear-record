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

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

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

# The desktop app build (ADR-0014, ADR-0016): the `app` recipe and the
# PyInstaller spec must keep shipping the tray, or a double-click silently
# degrades from a menu-bar home to a browser tab.
JUSTFILE = REPO_ROOT / "justfile"
PYINSTALLER_SPEC = REPO_ROOT / "packaging" / "pyinstaller" / "clear-record.spec"
TRAY_LAUNCHER = PYINSTALLER_SPEC.parent / "tray_launch.py"
CLI_LAUNCHER = PYINSTALLER_SPEC.parent / "cli_launch.py"

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
# while the web *code* still ships in this one wheel. `click` joined the base
# set with the CLI port (ADR-0022): it is pure Python, zero transitive deps.
# `platformdirs` joined with ADR-0025: the one platform-native path resolver,
# MIT, zero dependencies (imported at the package root, never in `core`).
# `alembic`/`sqlalchemy` joined with ADR-0030: Alembic owns the registry's schema
# and it migrates when the process opens it, and the `bench`/`diagnose` entry
# points ship in the base distribution and reach the registry — so unlike the
# web/tray/MCP stacks these cannot be an extra with an actionable hint.
# `pydantic` joined with the same ADR: the service layer validates the run
# options at the seam that reads them, so every base install (not just one with
# the `web` extra, whose FastAPI used to pull it in transitively) needs it.
# `json-repair` left the base set with ADR-0031: it existed only for the deleted
# in-process agent output contract.
RUNTIME_DEPS = {
    "alembic",
    "click",
    "numpy",
    "platformdirs",
    "pydantic",
    "soundfile",
    "sqlalchemy",
}

# The optional tool surfaces' dependency sets (ADR-0013, ADR-0016, ADR-0017).
# They must not leak into the base dependencies: a CLI-only install stays
# audio-only, while the web/MCP *code* still ships in this one wheel.
WEB_EXTRA_DEPS = {"fastapi", "uvicorn", "jinja2", "python-multipart"}
TRAY_EXTRA_DEPS = {"pyside6"}
AGENTS_EXTRA_DEPS = {"mcp"}


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
    """The one published dist depends only on its declared allow-list (ADR-0012).

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


def test_optional_surfaces_are_extras_not_base_dependencies() -> None:
    """The web/tray/MCP stacks are extras, not base dependencies (ADR-0013/0016/0017).

    Each surface ships inside this one wheel (no second dist to publish), but its
    dependencies must not: a CLI-only install stays audio-only. Every surface is
    still registered as a subcommand by an entry point, so it is discoverable
    without the CLI importing the surface module.
    """
    project = _load(MEMBER_PYPROJECTS[0])["project"]
    extras = project.get("optional-dependencies", {})
    names = {
        extra: {_NAME_RE.match(spec).group() for spec in specs}
        for extra, specs in extras.items()
    }
    assert names.get("web") == WEB_EXTRA_DEPS, names.get("web")
    assert names.get("tray") == TRAY_EXTRA_DEPS, names.get("tray")
    assert names.get("agents") == AGENTS_EXTRA_DEPS, names.get("agents")

    base = {_NAME_RE.match(spec).group() for spec in project.get("dependencies", [])}
    leaked = (WEB_EXTRA_DEPS | TRAY_EXTRA_DEPS | AGENTS_EXTRA_DEPS) & base
    assert not leaked, f"optional stack leaked into base dependencies: {sorted(leaked)}"

    declared = project.get("entry-points", {}).get("clear_record.commands", {})
    assert declared.get("web") == "clear_record.web:register", declared
    assert declared.get("tray") == "clear_record.tray:register", declared
    assert declared.get("mcp") == "clear_record.mcp:register", declared


# Every template the console ships today. The rglob scan below covers a newly
# added template (and flags a truncated one), but a *deleted* one simply drops
# out of the scan — this frozen set is what makes a deletion fail instead, which
# only holds while the set names every template on disk:
# `test_the_frozen_template_set_names_every_template_on_disk` asserts that parity
# in both directions, so a new template has to be frozen here to be guarded (and
# `activity.html` with `_activity_run.html` were the pair that had been missed).
EXPECTED_WEB_TEMPLATES = frozenset(
    {
        "_add_project.html",
        "_activity_run.html",
        "_agent_setup.html",
        "_archive_status.html",
        "_archives.html",
        "_backend_list.html",
        "_detail.html",
        "_harness_setup.html",
        "_hello_check.html",
        "_mcp_config.html",
        "_mcp_setup.html",
        "_meeting.html",
        "_profile_options.html",
        "_project_tabs.html",
        "_projects.html",
        "_run.html",
        "_settings_agent.html",
        "_settings_backends.html",
        "_settings_mcp.html",
        "_settings_models.html",
        "_settings_status.html",
        "_settings_storage.html",
        "_settings_webhooks.html",
        "_setup_transcription.html",
        "_storage.html",
        "_tab_glossary.html",
        "_tab_media.html",
        "_tab_meetings.html",
        "_tab_overview.html",
        "_upload.html",
        "_webhooks.html",
        "404.html",
        "409.html",
        "activity.html",
        "agent.html",
        "auth.html",
        "base.html",
        "index.html",
        "meeting.html",
        "project.html",
        "settings.html",
        "setup.html",
    }
)


def _missing_web_templates(templates: Path) -> list[str]:
    """Expected template names absent from a templates directory.

    Split out so a test can prove a deletion is caught, not just assumed.
    """
    present = {path.name for path in templates.rglob("*.html")}
    return sorted(EXPECTED_WEB_TEMPLATES - present)


def test_web_console_assets_ship_with_the_package() -> None:
    """The console's templates and compiled assets are package data.

    They must exist on disk (the wheel ships them; the frozen build collects
    them), so a rename that orphans the UI fails here rather than at runtime.
    The compiled CSS/JS come from the front-end build and are committed — see
    ADR-0023 and docs/frontend-assets.md.
    """
    web = PACKAGE_DIRS[0] / "src" / "clear_record" / "web"
    templates = web / "templates"
    # Derived from disk: a template is covered the moment it lands, and a
    # truncated one would render a blank page.
    committed = sorted(templates.rglob("*.html"))
    assert committed, f"no templates under {templates}"
    empty = [p.relative_to(REPO_ROOT) for p in committed if not p.read_text().strip()]
    assert not empty, f"empty web templates: {empty}"
    # Deletion guard: the scan alone cannot notice a missing file.
    missing_templates = _missing_web_templates(templates)
    assert not missing_templates, f"missing web templates: {missing_templates}"
    # The page-level routes ADR-0027 names, so a wholesale rename is caught.
    for name in (
        "base.html",
        "index.html",
        "project.html",
        "meeting.html",
        "settings.html",
        "setup.html",
        "agent.html",
    ):
        assert (templates / name).is_file(), f"missing web page template: {name}"
    missing = [
        rel for rel in ("static/app.css", "static/app.js") if not (web / rel).is_file()
    ]
    assert not missing, f"missing web console assets: {missing}"


@pytest.mark.parametrize(
    "victim",
    [
        # A partial: not one of the page anchors below, so only the frozen set
        # can notice it is gone.
        "_detail.html",
        # The Activity page and its row partial: the page is not in the
        # anchor list either, and its partial is not a page at all — both were
        # absent from the frozen set, so deleting either left the guard silent.
        "activity.html",
        "_activity_run.html",
    ],
)
def test_web_template_guard_catches_a_deleted_template(
    tmp_path: Path, victim: str
) -> None:
    """Removing a template only the frozen set covers must fail the guard.

    Each of these is invisible to the other two checks in
    :func:`test_web_console_assets_ship_with_the_package`: the rglob scan is
    derived from disk, and the page anchors name a fixed seven. The frozen set is
    what makes their deletion fail instead, which is the sentence it carries.
    """
    source = PACKAGE_DIRS[0] / "src" / "clear_record" / "web" / "templates"
    copied = tmp_path / "templates"
    shutil.copytree(source, copied)
    assert victim in EXPECTED_WEB_TEMPLATES
    (copied / victim).unlink()
    assert _missing_web_templates(copied) == [victim]


def test_the_frozen_template_set_names_every_template_on_disk() -> None:
    """The frozen set is exactly what ships — neither short nor stale.

    ``_missing_web_templates`` catches a *deletion* the rglob scan cannot see; this
    is the other direction, and without it the set can quietly stop guarding: a
    template added to the tree but left out of the set makes a later deletion of
    *that* template invisible (`activity.html` and `_activity_run.html` were in
    exactly that state). Compared against disk rather than against a second list,
    so the only way to keep the guard's sentence true is to freeze every template
    the console ships.
    """
    templates = PACKAGE_DIRS[0] / "src" / "clear_record" / "web" / "templates"
    on_disk = {path.name for path in templates.rglob("*.html")}
    assert on_disk == set(EXPECTED_WEB_TEMPLATES), (
        f"on disk but not frozen: {sorted(on_disk - EXPECTED_WEB_TEMPLATES)}; "
        f"frozen but not on disk: {sorted(EXPECTED_WEB_TEMPLATES - on_disk)}"
    )


# The registry's schema history is read at run time, never imported: the store
# names it as the string `clear_record.service:migrations`, so a revision or the
# environment falling out of the install breaks no import — it breaks the first
# open of a registry, in a distribution that has already launched. Frozen for
# the same reason the web assets are.
EXPECTED_MIGRATIONS = frozenset(
    {
        "env.py",
        "script.py.mako",
        "versions/0006_released_baseline.py",
        "versions/0007_run_ownership.py",
        "versions/0008_run_cancel_and_resume.py",
        "versions/0009_active_run_per_meeting.py",
    }
)


def _missing_migrations(migrations: Path) -> list[str]:
    """Expected schema-history files absent from a migrations directory.

    Split out so a test can prove a deletion is caught, not just assumed.
    """
    present = {
        path.relative_to(migrations).as_posix()
        for path in migrations.rglob("*")
        if path.is_file()
    }
    return sorted(EXPECTED_MIGRATIONS - present)


def test_the_schema_history_ships_with_the_package() -> None:
    """Alembic's script location resolves inside the installed package.

    The store names it as a package resource, so the registry of an installed
    distribution migrates at open with no configuration beside it (ADR-0030).
    """
    migrations = PACKAGE_DIRS[0] / "src" / "clear_record" / "service" / "migrations"
    missing = _missing_migrations(migrations)
    assert not missing, f"missing schema history: {missing}"


def test_schema_history_guard_catches_a_deleted_revision(tmp_path: Path) -> None:
    """Removing a revision must fail the guard, not pass silently.

    Nothing imports a revision, so only the expected-set check can notice it.
    """
    source = PACKAGE_DIRS[0] / "src" / "clear_record" / "service" / "migrations"
    copied = tmp_path / "migrations"
    shutil.copytree(source, copied)
    victim = "versions/0007_run_ownership.py"
    assert victim in EXPECTED_MIGRATIONS
    (copied / victim).unlink()
    assert _missing_migrations(copied) == [victim]


def _just_recipe(name: str) -> str:
    """Return a recipe's indented body, up to the next top-level entry."""
    body: list[str] = []
    lines = JUSTFILE.read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"{name}:"):
            continue
        for following in lines[index + 1 :]:
            if following.startswith((" ", "\t")):
                body.append(following.strip())
            elif following.strip():
                break
        break
    return "\n".join(body)


def test_web_assets_build_and_guard_are_just_recipes() -> None:
    """`just` owns both front-end entry points, and `verify` stays Node-less.

    The console's compiled assets are committed and rebuilt by `just
    web-assets`; `just web-assets-check` is the freshness guard (ADR-0023). The
    guard needs bun, so it must not be folded into `verify` — a contributor who
    never touches the UI can still run the Python gate.
    """
    assert "bun" in _just_recipe("web-assets")
    assert "check_web_assets.py" in _just_recipe("web-assets-check")
    assert "web-assets" not in _just_recipe("verify")
    assert (REPO_ROOT / "scripts" / "check_web_assets.py").is_file()


def test_message_catalogs_ship_with_the_package() -> None:
    """Every locale carries a committed `.po` **and** its compiled `.mo`.

    `gettext.translation` reads `messages.mo` out of the wheel, so a missing or
    uncompiled catalog silently degrades a locale to English. Static check only —
    no Babel, no build; the freshness guard is `just i18n-check` (its own CI
    job, because it needs Babel).
    """
    locales = PACKAGE_DIRS[0] / "src" / "clear_record" / "locales"
    catalogs = sorted(locales.glob("*/LC_MESSAGES/messages.po"))
    assert catalogs, f"no message catalogs under {locales}"
    missing_mo = [
        p.relative_to(REPO_ROOT) for p in catalogs if not p.with_suffix(".mo").is_file()
    ]
    assert not missing_mo, f"missing compiled catalogs: {missing_mo}"


def test_i18n_build_and_guard_are_just_recipes() -> None:
    """`just` owns the catalog entry points; `verify` stays Babel-free.

    The catalogs are committed and rebuilt by `just i18n-extract` /
    `i18n-compile`; `just i18n-check` is the freshness guard. It needs Babel, so
    it must not be folded into `verify` — the runtime is stdlib `gettext` and a
    contributor who never touches a translation can still run the Python gate.
    """
    assert "scripts/i18n.py extract" in _just_recipe("i18n-extract")
    assert "scripts/i18n.py compile" in _just_recipe("i18n-compile")
    assert "scripts/i18n.py check" in _just_recipe("i18n-check")
    assert "i18n" not in _just_recipe("verify")
    assert (REPO_ROOT / "scripts" / "i18n.py").is_file()


def test_e2e_is_a_just_recipe_guarded_by_the_provisioning_pointer_script() -> None:
    """`just e2e` checks its provisioning before it seeds, boots or launches.

    A freshly cut worktree has neither the frontend's `node_modules` nor the
    browser, and either one missing otherwise costs a diagnosis session:
    `playwright: command not found` (exit 127), or one `browserType.launch`
    failure per spec. The guard branches, so it is a pointer-script, and it
    shares the browser path with the run through `e2e_browsers_path` so the
    check cannot drift from the browser Playwright actually launches.
    """
    first, *_, launch = _just_recipe("e2e").splitlines()
    assert "uv run --no-project scripts/check_e2e_provisioning.py" in first, first
    assert "PLAYWRIGHT_BROWSERS_PATH={{e2e_browsers_path}}" in first, first
    assert "PLAYWRIGHT_BROWSERS_PATH={{e2e_browsers_path}}" in launch, launch
    assert (REPO_ROOT / "scripts" / "check_e2e_provisioning.py").is_file()


def test_e2e_provisioning_guard_reports_each_missing_piece(tmp_path: Path) -> None:
    """The guard exits non-zero naming the fix for each piece `e2e` needs.

    Run in a synthetic tree — the guard resolves the repo root from its own
    path — because whether the real tree has `node_modules` depends on who is
    running the suite, and both branches have to be pinned either way. A
    regression here is the failure mode this guard exists for: a fresh worktree's
    gate reporting one `browserType.launch` error per spec instead of one line.
    """
    guard = tmp_path / "scripts" / "check_e2e_provisioning.py"
    guard.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "check_e2e_provisioning.py", guard)
    browsers = tmp_path / "ms-playwright"
    browsers.mkdir()
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)}

    def check() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(guard)], capture_output=True, text=True, env=env
        )

    missing_deps = check()
    assert missing_deps.returncode != 0
    assert (
        "bun install --frozen-lockfile --cwd packages/clear-record/frontend"
        in missing_deps.stderr
    ), missing_deps.stderr

    runner = tmp_path / "packages/clear-record/frontend/node_modules/.bin/playwright"
    runner.parent.mkdir(parents=True)
    runner.touch()

    missing_browser = check()
    assert missing_browser.returncode != 0
    assert "just e2e-install" in missing_browser.stderr, missing_browser.stderr
    assert str(browsers) in missing_browser.stderr, missing_browser.stderr


def test_babel_is_a_build_only_group_not_a_runtime_dependency() -> None:
    """Babel extracts/compiles catalogs but is never imported at runtime.

    It lives in the root `i18n` dependency group (not `dev`, so the verify gate
    stays Babel-free) and in neither the base dependencies nor an optional extra
    of the published dist.
    """
    root = _load(ROOT_PYPROJECT)
    groups = root.get("dependency-groups", {})
    assert any("babel" in spec.lower() for spec in groups.get("i18n", [])), groups
    assert not any("babel" in spec.lower() for spec in groups.get("dev", []))

    project = _load(MEMBER_PYPROJECTS[0])["project"]
    leaked = [spec for spec in _all_dependencies(project) if "babel" in spec.lower()]
    assert not leaked, f"Babel leaked into the dist's dependencies: {leaked}"


def test_desktop_app_bundle_ships_the_tray_as_its_entry_point() -> None:
    """The frozen desktop app is the tray, not just a browser launcher (ADR-0016).

    Three literals have to move together or a double-click silently degrades to a
    browser tab: the `app` recipe pulls the `tray` extra (and `web`, which the
    tray supervises the console through), the spec does not exclude PySide6, and
    the macOS bundle's main executable is the tray launcher. Static text checks
    over the justfile and the spec — no PyInstaller run, no PySide6 download.
    """
    recipe = _just_recipe("app")
    assert "--extra web" in recipe, f"`just app` must pull the web extra: {recipe!r}"
    assert "--extra tray" in recipe, f"`just app` must pull the tray extra: {recipe!r}"

    spec = PYINSTALLER_SPEC.read_text()
    excludes = re.search(r"excludes=\[(.*?)\]", spec, re.S)
    assert excludes is not None, "spec has no excludes list"
    assert "PySide6" not in excludes.group(1), "the spec excludes PySide6"

    hidden = re.search(r"hiddenimports = \[(.*?)\]", spec, re.S)
    assert hidden is not None, "spec has no hiddenimports list"
    for module in (
        "clear_record.tray",
        "clear_record.tray.app",
        "clear_record.tray.service",
    ):
        assert module in hidden.group(1), f"missing hidden import {module!r}"

    assert TRAY_LAUNCHER.is_file(), f"missing frozen tray launcher: {TRAY_LAUNCHER}"
    assert 'str(SPEC_DIR / "tray_launch.py")' in spec, "spec omits the tray launcher"
    assert 'name="clear-record-tray"' in spec, "spec builds no clear-record-tray binary"
    assert '"CFBundleExecutable": "clear-record-tray"' in spec, (
        "the macOS bundle still points at a non-tray executable"
    )
    # The tray is the default, not the only path: the browser launcher and the
    # CLI must remain in the bundle.
    assert 'name="clear-record-web"' in spec
    assert 'name="clear-record"' in spec


def test_a_frozen_cli_starts_its_node_with_its_own_executable(monkeypatch) -> None:
    """The node argv names this process's own `serve`, interpreter or bundle alike.

    The installed CLI starts its node as ``python -m clear_record.cli.cli serve``.
    The PyInstaller build (ADR-0014) has neither an interpreter to name nor a
    module to run: ``sys.executable`` *is* the CLI, and the bundle's launcher —
    the same file the spec builds the `clear-record` binary from — feeds
    ``sys.argv[1:]`` straight to :func:`clear_record.cli.cli.main`, so the node
    child is ``<bundle> serve``. A ``-m`` there is a usage error the node dies of.
    Every shape is pinned statically; no frozen build is launched.
    """
    from clear_record.cli import cli

    assert cli._node_argv() == [sys.executable, "-m", "clear_record.cli.cli", "serve"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli._node_argv() == [sys.executable, "serve"]

    # The reason the frozen shape is `<exe> <subcommand>`: the launcher's whole
    # argv contract, and the spec building the CLI from that launcher.
    launcher = CLI_LAUNCHER.read_text()
    assert "sys.argv[1:]" in launcher, "the frozen launcher does not take raw argv"
    assert "main(argv" in launcher, "the frozen launcher does not call main(argv)"
    assert 'scripts["cli_launch"]' in PYINSTALLER_SPEC.read_text(), (
        "the spec no longer builds the CLI binary from cli_launch.py"
    )


def test_declares_the_clear_record_script() -> None:
    """The single dist owns the `clear-record` command (ADR-0009, ADR-0012).

    The entry point targets the CLI implementation directly — the old
    `clear_record.cli:main` alias is gone — so `import clear_record` stays
    light; the old `clearrecord` spelling must not reappear."""
    scripts = _load(MEMBER_PYPROJECTS[0])["project"]["scripts"]
    assert scripts.get("clear-record") == "clear_record.cli.cli:main"
    assert "clearrecord" not in scripts


def test_entry_point_resolves_to_the_cli_main() -> None:
    """The declared entry point actually reaches the CLI implementation.

    The target is read back out of the manifest and resolved, so renaming the
    module or the attribute fails here instead of at first run."""
    import importlib

    target = _load(MEMBER_PYPROJECTS[0])["project"]["scripts"]["clear-record"]
    module_name, _, attribute = target.partition(":")
    resolved = getattr(importlib.import_module(module_name), attribute)

    from clear_record.cli.cli import main

    assert resolved is main


def test_import_clear_record_is_light() -> None:
    """Importing the top-level package must not pull in the heavy CLI stack.

    The console script targets `clear_record.cli.cli:main` precisely so that a
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


# --- packaging docs and metadata agree with the build (round-2 guards) ------- #

NOTICES = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
FLATPAK_README = REPO_ROOT / "packaging" / "flatpak" / "README.md"
FLATPAK_METAINFO = (
    REPO_ROOT / "packaging" / "flatpak" / "io.github.zhaoweny.clear-record.metainfo.xml"
)
FLATPAK_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-flatpak.yml"
FLATPAK_ADR = REPO_ROOT / "docs" / "adr" / "0015-flatpak-linux-distribution.md"
PYINSTALLER_README = PYINSTALLER_SPEC.parent / "README.md"


def _notices_table(heading: str) -> set[str]:
    """Distribution names in the pipe table under one notices heading."""
    text = NOTICES.read_text()
    match = re.search(
        rf"### {re.escape(heading)}.*?\n(.*?)(?=\n### |\n## )", text, re.S
    )
    assert match is not None, f"no {heading!r} section in {NOTICES.name}"
    return set(re.findall(r"^\| \[`([^`]+)`\]", match.group(1), re.M))


def test_third_party_notices_cover_every_base_runtime_dependency() -> None:
    """Every base runtime dep is documented (the file claims uv.lock parity)."""
    runtime = _notices_table("Runtime dependencies (installed by default)")
    undocumented = RUNTIME_DEPS - runtime
    assert not undocumented, f"undocumented runtime deps: {sorted(undocumented)}"


def test_notices_do_not_call_a_runtime_web_dependency_build_only() -> None:
    """Jinja2 is a web-extra runtime dep, so it is not build-only.

    Babel and PyInstaller genuinely are build tools; listing a runtime package
    in that table would understate what an installed console links.
    """
    build_only = _notices_table("Build-only dependencies")
    assert {"babel", "pyinstaller"} <= build_only, build_only
    assert "jinja2" not in build_only, "Jinja2 is a runtime dependency of web"


def test_flatpak_lane_trigger_matches_its_documentation() -> None:
    """The workflow trigger and the flatpak prose cannot drift apart."""
    workflow = FLATPAK_WORKFLOW.read_text()
    readme = FLATPAK_README.read_text()
    adr = FLATPAK_ADR.read_text()
    tag_triggered = re.search(r"^  push:", workflow, re.M) is not None
    if tag_triggered:
        assert "tags:" in workflow
    else:
        assert "manual dispatch" in readme, "README must state the lane is manual"
        assert "on `v*` tags" not in readme, "README still claims a tag trigger"
        assert "on `v*` tags" not in adr, "ADR-0015 still claims a tag trigger"
    assert "Python strings" not in readme, "frontend is package data (ADR-0016/0023)"
    assert "upload-artifact@v7" in readme, "README names a stale artifact action"


def test_flatpak_generator_is_pinned_to_a_commit() -> None:
    """The generator is fetched from a commit, using the real script path.

    The extensionless pip/flatpak-pip-generator is a git symlink; raw
    .githubusercontent serves the link target text (flatpak-pip-generator.py),
    so a pin to that path still downloads a non-script.
    """
    workflow = FLATPAK_WORKFLOW.read_text()
    assert "flatpak-builder-tools/master" not in workflow, "fetched from master"
    script = r"flatpak-builder-tools/[0-9a-f]{40}/pip/flatpak-pip-generator\.py"
    assert re.search(script, workflow), (
        "flatpak-pip-generator must be the commit-pinned .py script"
    )
    symlink = r"flatpak-builder-tools/[0-9a-f]{40}/pip/flatpak-pip-generator(?!\.py)"
    assert not re.search(symlink, workflow), (
        "the extensionless path is a git symlink, not the script"
    )


def test_appstream_metadata_tracks_the_release_version() -> None:
    """The AppStream release list is not stale against the manifest version."""
    version = _load(MEMBER_PYPROJECTS[0])["project"]["version"]
    core = re.match(r"^\d+\.\d+\.\d+", version).group()
    releases = re.findall(r'<release version="([^"]+)"', FLATPAK_METAINFO.read_text())
    assert releases, "metainfo declares no <release>"
    cores = {
        m.group() for r in releases if (m := re.match(r"^\d+\.\d+\.\d+", r)) is not None
    }
    assert core in cores, f"no AppStream release for {core}: {releases}"


def test_desktop_bundle_scope_is_documented() -> None:
    """If the frozen app omits the MCP provider, the README must say so."""
    spec = PYINSTALLER_SPEC.read_text()
    hidden = re.search(r"hiddenimports = \[(.*?)\]", spec, re.S)
    bundled = bool(hidden) and "clear_record.mcp" in hidden.group(1)
    readme = PYINSTALLER_README.read_text()
    if not bundled:
        assert "clear-record mcp" in readme, "README must name the omitted command"
        assert "agents" in readme, "README must name the extra supplying MCP"
