"""The tray's status line, tested without Qt.

clear_record.tray.app imports PySide6 only inside main(), so the live state to
status string mapping is headless; only the QAction/tooltip repaint is not. The
state reads themselves are covered in test_service.py; the suite's autouse
fixture pins the English default, so these assertions are exact.
"""

from __future__ import annotations

import gettext
from types import SimpleNamespace

from clear_record.core import i18n
from clear_record.tray.app import status_text
from clear_record.tray.service import ServiceState

URL = "http://127.0.0.1:8765/web/"


def _controller(state: ServiceState) -> SimpleNamespace:
    """The two attributes the status line reads, as the real controller has them."""
    return SimpleNamespace(state=lambda: state, console_url=URL)


class _Pseudo(gettext.NullTranslations):
    """A transforming catalog: proves each string goes through tr()."""

    def gettext(self, message: str) -> str:
        return f"[{message}]"


def test_running_names_the_url() -> None:
    assert status_text(_controller(ServiceState.RUNNING)) == f"Running at {URL}"


def test_unreachable_and_stopped_read_differently() -> None:
    assert status_text(_controller(ServiceState.UNREACHABLE)) == "Not responding"
    assert status_text(_controller(ServiceState.STOPPED)) == "Server not running"


def test_status_line_goes_through_the_catalog() -> None:
    i18n.use(_Pseudo())
    assert status_text(_controller(ServiceState.RUNNING)) == f"[Running at {URL}]"
    assert status_text(_controller(ServiceState.UNREACHABLE)) == "[Not responding]"
