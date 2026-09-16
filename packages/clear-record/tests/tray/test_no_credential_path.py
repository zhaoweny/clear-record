"""The tray has no credential path: it supervises and opens a URL.

With auth enabled the **browser** is what asks for the password, so the tray
never loads, stores or forwards one (SET-04). The guard is a source scan of
:mod:`clear_record.tray`:

- credential nouns in names, attributes, parameter names, import aliases,
  keyword arguments and string data, matched as **whole words** with ``_`` and
  ``-`` as separators (``X-Auth``, ``load_credential``) — never as substrings, so
  ``Signing`` is not ``sign-in`` and ``author`` is not ``auth``;
- a bare ``key``/``keys`` identifier or an assignment target that contains one
  (``tray_key``);
- an environment-variable-shaped credential name in a string (``CR_TRAY_KEY``);
- the URL the tray opens and probes is a bare origin: no userinfo and no query,
  so a token cannot ride along in it.

Docstrings are excluded — prose may describe the rule. A positive control drives
the same scanner with known-bad snippets, so an edit that makes the scanner
vacuous fails the suite.
"""

from __future__ import annotations

import ast
import re
import urllib.parse
from pathlib import Path

import pytest

from clear_record.tray.service import ServiceController

TRAY_SRC = Path(__file__).resolve().parents[2] / "src" / "clear_record" / "tray"

#: Credential nouns. The boundaries treat ``_``/``-`` as separators, so
#: ``X-Auth`` and ``load_credential`` split, while ``Signing`` and ``author`` do not.
CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"password|passwd|passphrase|credential|secret|token|bearer|cookie|"
    r"session|login|sign[_-]?in|api[_-]?key|auth|authoriz\w*|authenticat\w*"
    r")s?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

#: A bare ``key``/``keys`` identifier — a parameter, argument or binding.
BARE_KEY = re.compile(r"key|keys", re.IGNORECASE)

#: A word-``key`` inside an assignment target (``tray_key``, ``api_key``).
ASSIGNED_KEY = re.compile(r"(?<![A-Za-z0-9])(?:key|keys)(?![A-Za-z0-9])", re.IGNORECASE)

#: An environment-variable-shaped credential name (``CR_TRAY_KEY``) in a string.
ENV_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Z][A-Z0-9_]*_(?:KEY|KEYS|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|"
    r"COOKIE|SESSION)"
    r"(?![A-Za-z0-9])"
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


def _name_texts(node: ast.AST) -> list[str]:
    """The name-like texts a node contributes: identifiers, aliases, parameters."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.alias):
        return [part for part in (node.name, node.asname) if part]
    if isinstance(node, ast.keyword):
        return [node.arg] if node.arg else []
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.module else []
    return []


def _target_names(target: ast.expr) -> list[str]:
    """The names one assignment target binds, unpacking tuples and subscripts."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, (ast.Starred, ast.Subscript)):
        return _target_names(target.value)
    return []


def _assigned_names(node: ast.AST) -> list[str]:
    """The names an assignment/annotation/augmented assignment binds."""
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    else:
        return []
    return [name for target in targets for name in _target_names(target)]


def scan(source: str, filename: str = "<source>") -> list[str]:
    """Every credential-shaped piece of ``source``, as ``location: what`` lines."""
    tree = ast.parse(source, filename=filename)
    docstrings = _docstrings(tree)
    findings: list[str] = []

    def flag(lineno: int, text: str, matched: str) -> None:
        findings.append(f"{filename}:{lineno}: {text!r} contains {matched!r}")

    for node in ast.walk(tree):
        for text in _name_texts(node):
            match = CREDENTIAL.search(text)
            if match:
                flag(node.lineno, text, match.group(0))
            elif BARE_KEY.fullmatch(text):
                flag(node.lineno, text, text)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for text in _assigned_names(node):
                match = ASSIGNED_KEY.search(text)
                if match:
                    flag(node.lineno, text, match.group(0))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            for pattern in (CREDENTIAL, ENV_CREDENTIAL):
                for match in pattern.finditer(node.value):
                    flag(node.lineno, node.value, match.group(0))
    return findings


#: Snippets that must each be flagged: the shapes a credential would arrive in.
_BAD = {
    "credential parameter": """
def probe(token):
    return run(probe, token)
""",
    "bare key parameter and argument": """
def probe(key):
    return run(probe, key)
""",
    "env key in a request header": """
import os

def probe(request):
    request.add_header("X-Auth", os.environ["CR_TRAY_KEY"])
""",
    "aliased credential import": """
from clear_record.web.auth import load_credential as lc
""",
    "assigned key": """
tray_key = read_config()
""",
    "session object": """
session = open_session()
""",
    "cookie jar": """
cookie_jar = load_cookies()
""",
    "bearer header": """
headers = {"Authorization": "Bearer x"}
""",
    "passwd parameter": """
def connect(passwd):
    return passwd
""",
}

#: Ordinary tray-shaped code that must stay clean — including the word
#: "Signing", which is not "sign in", and a ``{"key": ...}`` data literal.
_GOOD = """
import socket

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

def label() -> str:
    # "Signing" is a word of its own, not "sign in".
    return tr("Signing requests with the console")

def row() -> dict:
    return {"key": "value", "author": "the author"}
"""


@pytest.mark.parametrize("case", sorted(_BAD), ids=sorted(_BAD))
def test_scanner_flags_each_known_bad_shape(case: str) -> None:
    """Positive control: the scan is not vacuous — every shape is caught."""
    assert scan(_BAD[case]), f"scanner missed {case!r}"


def test_scanner_leaves_ordinary_code_alone() -> None:
    """The false positives the whole-word match removes must stay removed."""
    assert scan(_GOOD) == []


def test_tray_sources_name_no_credential() -> None:
    paths = sorted(TRAY_SRC.rglob("*.py"))
    assert paths, f"no tray sources found under {TRAY_SRC}"
    findings: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        findings.extend(scan(source, str(path.relative_to(TRAY_SRC.parent.parent))))
    assert not findings, (
        "the tray must have no credential path — with auth enabled the browser "
        "asks for the password. Move credential handling to the web layer, or "
        "reword if this is prose:\n" + "\n".join(findings)
    )


def test_tray_url_carries_no_credential(tmp_path) -> None:
    controller = ServiceController(port=8765, data_dir=str(tmp_path))
    parsed = urllib.parse.urlsplit(controller.url)
    assert parsed.scheme == "http"
    assert not parsed.username and not parsed.password
    assert not parsed.query and not parsed.fragment
