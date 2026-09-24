"""The PySide6 system-tray app: the desktop entry point of the console.

A thin shell over :class:`clear_record.tray.service.ServiceController`. The
icon has four jobs: open the console in a browser, show whether the node is
running **right now**, restart it, and quit. The tray **joins** a node that is
already up rather than starting a second one, so "restart" and "quit" are about
the node this tray started: a node it only joined is left running
(:meth:`~clear_record.tray.service.ServiceController.supervises` is what the
menu offers restart from). No icon asset ships — a standard style icon is used
so the build carries no binary art.

The tray holds no credential: it supervises the node it starts, joins one it
finds, and opens the console URL; with auth enabled the **browser** is what asks
for the password.

Requires the `tray` extra (PySide6) — imported inside :func:`main`, so this
module stays importable without Qt. The Qt shell itself is not covered by the
headless verify gate; the supervision logic it wraps is (see ``tests/tray/``).
"""

from __future__ import annotations

import sys
import webbrowser

from clear_record.core.i18n import tr
from clear_record.core.node import DEFAULT_HOST, DEFAULT_PORT
from clear_record.tray.service import ServiceController, ServiceState

#: How often the tray re-reads the live state. Each read is one local health
#: request (1 s socket timeout), so the event loop stalls at most briefly — and
#: only while the server is wedged.
STATE_POLL_MS = 2000


def status_text(controller: ServiceController) -> str:
    """The live status line for a fresh state read — never a cached snapshot.

    The state is the node's own health (:meth:`ServiceController.state`), which
    is the same question whether this tray started that node or joined one that
    was already up.
    """
    state = controller.state()
    if state is ServiceState.RUNNING:
        return tr("Running at {url}", url=controller.url)
    if state is ServiceState.UNREACHABLE:
        return tr("Not responding")
    return tr("Server not running")


def main(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    data_dir: str | None = None,
    open_browser: bool = True,
) -> int:
    from PySide6.QtCore import QTimer
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
    # A node already up is joined rather than started a second time; only when
    # none answers does the tray start one of its own.
    controller.start()
    ready = controller.wait_until_ready()

    tray = QSystemTrayIcon(
        qt.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    )

    menu = QMenu()
    open_action = QAction(tr("Open console"), menu)
    open_action.triggered.connect(lambda: webbrowser.open(controller.url))
    menu.addAction(open_action)

    status_action = QAction(menu)
    status_action.setEnabled(False)
    menu.addAction(status_action)

    menu.addSeparator()

    restart_action = QAction(tr("Restart server"), menu)
    restart_action.triggered.connect(lambda: controller.restart())
    # Only a node this tray started is its to restart: the action is offered
    # greyed out for a node it only joined, because `restart` answers `False`
    # for a node this process does not own.
    restart_action.setEnabled(controller.supervises)
    menu.addAction(restart_action)

    menu.addSeparator()

    def quit_app() -> None:
        controller.stop()
        tray.hide()
        qt.quit()

    quit_action = QAction(tr("Quit"), menu)
    quit_action.triggered.connect(quit_app)
    menu.addAction(quit_action)

    def refresh() -> None:
        """Repaint the live state; called once now, then on every poll tick."""
        status = status_text(controller)
        status_action.setText(status)
        tray.setToolTip(tr("clear-record console — {status}", status=status))

    tray.setContextMenu(menu)
    refresh()
    tray.show()

    poll = QTimer()
    poll.setInterval(STATE_POLL_MS)
    poll.timeout.connect(refresh)
    poll.start()

    if open_browser and ready:
        webbrowser.open(controller.url)

    try:
        return qt.exec()
    finally:
        controller.stop()


__all__ = ["main"]
