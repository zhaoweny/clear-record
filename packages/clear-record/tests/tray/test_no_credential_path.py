"""The tray has no credential path: it supervises and opens a URL.

With auth enabled the **browser** is what asks for the password, so the tray
never loads, stores or forwards one (SET-04). Two guards:

- a source scan of :mod:`clear_record.tray` for credential nouns — in
  identifiers, attributes and string data, but not in docstrings, since prose
  is allowed to describe the rule;
- the URL the tray opens and probes is a bare origin: no userinfo, no query, so
  a token cannot ride along in it.
"""

from __future__ import annotations

import ast
import re
import urllib.parse
from pathlib import Path

from clear_record.tray.service import ServiceController

TRAY_SRC = Path(__file__).resolve().parents[2] / "src" / "clear_record" / "tray"

#: Credential-ish nouns. ``authoriz`` also catches an ``Authorization`` header.
CREDENTIAL = re.compile(
    r"password|passphrase|credential|secret|api[_-]?key|token|login|sign[-_ ]?in|authoriz",
    re.IGNORECASE,
)


def _docstrings(tree: ast.AST) -> set[int]:
    """The ``id()``s of every docstring constant (prose, not a code path)."""
    found: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, owners) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _text_data(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstrings(tree)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            found.append((node.attr, node.lineno))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append((node.value, node.lineno))
    return found


def test_tray_sources_name_no_credential() -> None:
    paths = sorted(TRAY_SRC.rglob("*.py"))
    assert paths, f"no tray sources found under {TRAY_SRC}"
    offenders: list[str] = []
    for path in paths:
        for text, lineno in _text_data(path):
            match = CREDENTIAL.search(text)
            if match:
                where = path.relative_to(TRAY_SRC.parent.parent)
                offenders.append(
                    f"{where}:{lineno}: {text!r} matches {match.group(0)!r}"
                )
    assert not offenders, (
        "the tray must have no credential path — with auth enabled the browser "
        "asks for the password. Move credential handling to the web layer, or "
        "reword if this is prose:\n" + "\n".join(offenders)
    )


def test_tray_url_carries_no_credential(tmp_path) -> None:
    controller = ServiceController(port=8765, data_dir=str(tmp_path))
    parsed = urllib.parse.urlsplit(controller.url)
    assert parsed.scheme == "http"
    assert not parsed.username and not parsed.password
    assert not parsed.query and not parsed.fragment
