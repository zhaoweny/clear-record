"""The release-train bump scripts share one implementation.

`bump_dev.py` and `bump_rc.py` own different transitions, but the version
read/write pair and the shapes they split on live in the shared
`scripts/_release_version` helper. These tests pin the sharing (so the
duplication cannot creep back) and the transition table documented in
docs/releasing.md.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"


@pytest.fixture
def release_scripts(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], types.ModuleType]:
    """Import a script as a top-level module, then drop it after the test.

    The scripts are not a package, so they are imported exactly the way uv runs
    them: with `scripts/` on `sys.path`.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    loaded: list[str] = []

    def load(name: str) -> types.ModuleType:
        module = importlib.import_module(name)
        loaded.append(name)
        return module

    yield load
    for name in loaded:
        sys.modules.pop(name, None)


def test_both_scripts_read_the_version_through_the_shared_helper(
    release_scripts,
) -> None:
    shared = release_scripts("_release_version")
    dev = release_scripts("bump_dev")
    rc = release_scripts("bump_rc")

    assert dev.current_version is shared.current_version
    assert rc.current_version is shared.current_version
    assert dev.run_bump is shared.run_bump
    assert rc.run_bump is shared.run_bump


def test_bump_dev_transitions(release_scripts) -> None:
    dev = release_scripts("bump_dev")

    assert dev.next_version("0.2.0.dev3") == "0.2.0.dev4"
    assert dev.next_version("0.2.0rc3.dev0") == "0.2.0rc3.dev1"
    assert dev.next_version("0.2.0rc3") == "0.2.0rc4.dev0"
    with pytest.raises(SystemExit, match="set-version"):
        dev.next_version("0.2.0")


def test_bump_rc_transitions(release_scripts) -> None:
    rc = release_scripts("bump_rc")

    assert rc.next_version("0.2.0.dev3") == "0.2.0rc1"
    assert rc.next_version("0.2.0rc3.dev0") == "0.2.0rc3"
    assert rc.next_version("0.2.0rc3") == "0.2.0rc4"
    with pytest.raises(SystemExit, match="set-version"):
        rc.next_version("0.2.0")


def test_the_justfile_header_documents_the_no_project_rule() -> None:
    """The header and the recipes must describe the same command shape."""
    header = (
        (REPO_ROOT / "justfile").read_text(encoding="utf-8").split("default:", 1)[0]
    )

    assert "uv run --no-project <file>.py" in header
    assert (SCRIPTS / "_release_version.py").is_file()
