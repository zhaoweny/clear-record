"""The PySide6 system-tray app: the desktop entry point of the console.

A thin shell over :class:`clear_record.tray.service.ServiceController`. The
icon has three jobs: open the console in a browser, show where it is listening,
and quit (stopping the background server). No icon asset ships — a standard
style icon is used so the build carries no binary art.

Requires the `tray` extra (PySide6). Not covered by the headless verify gate;
the supervision logic it wraps is (see ``tests/tray/test_service.py``).
"""

from __future__ import annotations

import sys
import webbrowser

from clear_record.tray.service import ServiceController


def main(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    data_dir: str | None = None,
    open_browser: bool = True,
) -> int:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QApplication,
        QMenu,
        QStyle,
        QSystemTrayIcon,
    )

    qt = QApplication.instance() or QApplication(sys.argv[:1])

    if not QSystemTrayIcon.isSystemTrayAvailable():
        raise SystemExit(
            "[tray] no system tray is available on this desktop.\n"
            "  Run the console without the tray instead:  clear-record web"
        )

    controller = ServiceController(host=host, port=port, data_dir=data_dir)
    controller.start()
    ready = controller.wait_until_ready()

    tray = QSystemTrayIcon(
        qt.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    )
    tray.setToolTip(f"clear-record console — {controller.url}")

    menu = QMenu()
    open_action = QAction("Open console", menu)
    open_action.triggered.connect(lambda: webbrowser.open(controller.url))
    menu.addAction(open_action)

    status_text = (
        f"Running at {controller.url}" if ready else "Not responding — see the console"
    )
    status_action = QAction(status_text, menu)
    status_action.setEnabled(False)
    menu.addAction(status_action)

    menu.addSeparator()

    def quit_app() -> None:
        controller.stop()
        tray.hide()
        qt.quit()

    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(quit_app)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()

    if open_browser and ready:
        webbrowser.open(controller.url)

    try:
        return qt.exec()
    finally:
        controller.stop()


__all__ = ["main"]
