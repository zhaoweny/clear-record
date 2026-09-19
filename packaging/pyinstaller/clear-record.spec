# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the clear-record desktop app.

Produces, from one analysis:

    dist/clear-record/clear-record-tray  the double-click target: runs the
                                         console with a menu-bar tray icon
    dist/clear-record/clear-record-web   starts the console and opens a browser
    dist/clear-record/clear-record       the full console CLI
    dist/clear-record.app                (macOS only) the above as an app bundle

The web console's frontend is package data — Jinja templates plus the built
CSS/JS — so it is collected explicitly. The web/service and tray modules are
imported only *dynamically* (through the `clear_record.commands` entry point and
lazy imports), so they are listed as hidden imports; `copy_metadata("clear-record")`
keeps the entry point resolvable inside the frozen app, which is what makes
`clear-record web` and `clear-record tray` work there too. Because
`clear_record.tray.app` is analyzed here, its deferred `PySide6` imports are seen
and the PyInstaller PySide6 hooks collect the Qt plugins and libraries.

Unsigned: see packaging/pyinstaller/README.md for the macOS Gatekeeper and
Windows SmartScreen notes. See docs/adr/0014-desktop-app-distribution.md.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parents[1]

# The app bundle version tracks the published dist (no second literal to drift).
with (ROOT / "packages" / "clear-record" / "pyproject.toml").open("rb") as _fh:
    VERSION = tomllib.load(_fh)["project"]["version"]

# The web/service and tray layers are reached dynamically, so modulegraph cannot
# see them; name them explicitly or the frozen app loses `web`/`tray`. The tray
# shell imports PySide6 *inside* its entry function, so none of that is visible
# until `clear_record.tray.app` is on the graph — the PyInstaller PySide6 hooks
# then take over and collect the Qt plugins and libraries.
hiddenimports = [
    "clear_record.service",
    "clear_record.service.runs",
    "clear_record.web",
    "clear_record.web.app",
    "clear_record.tray",
    "clear_record.tray.app",
    "clear_record.tray.service",
    "jinja2",
    *collect_submodules("uvicorn"),
    "anyio._backends._asyncio",
    "soundfile",
    "_soundfile",
]

datas = [
    *collect_data_files("soundfile"),
    # The console's frontend (Jinja templates + the built app.css/app.js) is
    # package data; without this the frozen app serves a blank page.
    *collect_data_files("clear_record"),
    # Dist metadata so `importlib.metadata.entry_points(group="clear_record.commands")`
    # still finds the bundled `web`/`tray` providers after freezing.
    *copy_metadata("clear-record"),
]
binaries = collect_dynamic_libs("soundfile")

a = Analysis(
    [
        str(SPEC_DIR / "tray_launch.py"),
        str(SPEC_DIR / "web_launch.py"),
        str(SPEC_DIR / "cli_launch.py"),
    ],
    pathex=[str(ROOT / "packages" / "clear-record" / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the bundle lean: none of these is used by the console, the CLI or the
    # tray. PySide6 is *not* excluded — the tray needs it.
    excludes=[
        "tkinter",
        "matplotlib",
        "PIL",
        "pytest",
        "IPython",
        "PyQt5",
        "PyQt6",
        "PySide2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

scripts = {Path(entry[1]).stem: entry for entry in a.scripts}

# The tray launcher is declared first so it becomes the macOS app bundle's main
# executable (double-click = menu-bar home); the web launcher and the CLI sit
# beside it as the other, still-supported paths.
tray_exe = EXE(
    pyz,
    [scripts["tray_launch"]],
    [],
    exclude_binaries=True,
    name="clear-record-tray",
    console=False,
    disable_windowed_traceback=False,
)
web_exe = EXE(
    pyz,
    [scripts["web_launch"]],
    [],
    exclude_binaries=True,
    name="clear-record-web",
    console=False,
    disable_windowed_traceback=False,
)
cli_exe = EXE(
    pyz,
    [scripts["cli_launch"]],
    [],
    exclude_binaries=True,
    name="clear-record",
    console=True,
)

coll = COLLECT(
    tray_exe,
    web_exe,
    cli_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="clear-record",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="clear-record.app",
        icon=None,
        bundle_identifier="io.github.zhaoweny.clear-record",
        info_plist={
            "CFBundleName": "clear-record",
            "CFBundleDisplayName": "clear-record",
            # Double-click = the menu-bar tray (ADR-0016). The web launcher and
            # the CLI remain in the bundle, but the tray is the default.
            "CFBundleExecutable": "clear-record-tray",
            "CFBundleShortVersionString": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
        },
    )
