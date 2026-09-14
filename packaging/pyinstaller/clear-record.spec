# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the clear-record desktop app.

Produces, from one analysis:

    dist/clear-record/clear-record-web   the double-click target: starts the
                                         local console and opens a browser
    dist/clear-record/clear-record       the full console CLI
    dist/clear-record.app                (macOS only) the above as an app bundle

The web console's frontend is embedded as Python strings, so there are no UI
data files to collect; the collections below cover the audio stack and the web
stack. The web/service modules are imported only *dynamically* (through the
`clear_record.commands` entry point and a lazy import), so they are listed as
hidden imports; `copy_metadata("clear-record")` keeps the entry point resolvable
inside the frozen app, which is what makes `clear-record web` work there too.

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

# The web/service layers are reached dynamically, so modulegraph cannot see
# them; name them explicitly or the frozen app loses `web`.
hiddenimports = [
    "clear_record.service",
    "clear_record.web",
    "clear_record.web.app",
    "clear_record.web.assets",
    *collect_submodules("uvicorn"),
    "anyio._backends._asyncio",
    "soundfile",
    "_soundfile",
]

datas = [
    *collect_data_files("soundfile"),
    # Dist metadata so `importlib.metadata.entry_points(group="clear_record.commands")`
    # still finds the bundled `web` provider after freezing.
    *copy_metadata("clear-record"),
]
binaries = collect_dynamic_libs("soundfile")

a = Analysis(
    [
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
    # Keep the bundle lean: nothing here is used by the console or the CLI.
    excludes=[
        "tkinter",
        "matplotlib",
        "PIL",
        "pytest",
        "IPython",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

scripts = {Path(entry[1]).stem: entry for entry in a.scripts}

# The web launcher is declared first so it becomes the macOS app bundle's main
# executable (double-click opens the console); the CLI sits beside it.
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
            # Double-click opens the console, so the web launcher (not the CLI)
            # is the bundle's main executable.
            "CFBundleExecutable": "clear-record-web",
            "CFBundleShortVersionString": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
        },
    )
