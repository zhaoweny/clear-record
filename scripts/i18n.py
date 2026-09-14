#!/usr/bin/env python3
"""Message-catalog toolchain for the console and CLI (Babel, build-time only).

Three entry points, behind the `just` recipes of the same names:

- ``extract`` — re-extract the English message IDs from Python and Jinja2
  sources and merge them into each locale's ``messages.po`` (new strings appear
  untranslated; removed ones are marked obsolete by Babel).
- ``compile`` — compile every ``messages.po`` into the committed ``messages.mo``.
- ``check`` — the freshness guard: re-extract and re-compile **without touching
  the tree** and fail when the committed catalogs differ from source. CI runs it
  as its own job, so a contributor without Babel can still run ``just verify``.

The runtime never uses Babel: ``clear_record.core.i18n`` is stdlib ``gettext``.
Babel is declared only in the ``i18n`` dependency group (see root
``pyproject.toml``), and ``pybabel`` is called as a subprocess here.

``tr``/``trn`` are the extraction keywords — Babel does not know them by default,
which is the whole reason the shorthand needs a build step. See ``docs/i18n.md``
and ``babel.cfg``.

The guard compares message **entries** and compiled ``.mo`` bytes rather than
raw files: the ``POT-Creation-Date`` header changes on every extraction, so a
byte-for-byte ``git status`` check (the `web-assets-check` shape) would never be
clean. Comparing the catalogs' contents is the same guarantee without the churn.
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "packages" / "clear-record"
BABEL_CFG = PACKAGE / "babel.cfg"
LOCALES_DIR = PACKAGE / "src" / "clear_record" / "locales"
SOURCE_ROOT = PACKAGE / "src" / "clear_record"
DOMAIN = "messages"

#: The shorthand Babel must be told about. ``trn:1,2`` names the first two
#: arguments as the singular/plural pair (the third, the count, is the call
#: site's). Babel's defaults cover ``_``/``gettext``/``ngettext``; ``tr``/``trn``
#: are ours, so they are passed explicitly on the command line.
KEYWORDS = ("tr", "trn:1,2", "deferred")


def _pybabel(*args: str) -> None:
    exe = shutil.which("pybabel")
    if exe is None:
        raise SystemExit(
            "i18n: `pybabel` is not on PATH.\n"
            "Run the catalog commands through `just i18n-extract` / "
            "`i18n-compile` / `i18n-check` (the `i18n` dependency group).\n"
            "`just verify` does not need Babel."
        )
    result = subprocess.run([exe, *args], cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"i18n: `pybabel {args[0]}` failed ({result.returncode})")
    # `pybabel extract` narrates every file it reads; keep our own output quiet.
    for line in result.stdout.splitlines():
        if line and not line.startswith("extracting messages from"):
            print(line)


def _extract_pot(dest: Path) -> None:
    """Write a fresh ``.pot`` to ``dest`` from the Python + Jinja2 sources.

    The source root is passed **relative to the repo** (``pybabel`` runs with
    ``cwd=REPO_ROOT``) so the ``#:`` location comments in the catalog stay
    portable rather than embedding a machine-specific absolute path.
    """
    source = SOURCE_ROOT.relative_to(REPO_ROOT).as_posix()
    args = ["extract", "-F", str(BABEL_CFG.relative_to(REPO_ROOT))]
    for keyword in KEYWORDS:
        args += ["--keyword", keyword]
    args += ["-o", str(dest), source]
    _pybabel(*args)


def _locale_dirs() -> list[Path]:
    if not LOCALES_DIR.is_dir():
        return []
    return sorted(
        child
        for child in LOCALES_DIR.iterdir()
        if (child / "LC_MESSAGES" / f"{DOMAIN}.po").is_file()
    )


def _message_ids(path: Path) -> set[object]:
    """The message IDs in a catalog, excluding the header and obsolete entries."""
    with path.open("rb") as handle:
        catalog = read_po(handle)
    ids: set[object] = set()
    for message in catalog:
        if not message.id:
            continue  # the header entry
        if getattr(message, "obsolete", False):
            continue
        ids.add(message.id)
    return ids


def _compiled_bytes(po_path: Path) -> bytes:
    """Compile one ``.po`` the way ``pybabel compile`` does, into memory."""
    with po_path.open("rb") as handle:
        catalog = read_po(handle)
    buffer = io.BytesIO()
    write_mo(buffer, catalog)
    return buffer.getvalue()


def extract() -> int:
    """Re-extract source strings and merge them into every locale's ``.po``."""
    if not _locale_dirs():
        print(
            f"i18n-extract: no locales under {LOCALES_DIR}.\n"
            "Create one first (from the repo root):\n"
            f"  pybabel init -i <messages.pot> -d {LOCALES_DIR} -l <lang>",
            file=sys.stderr,
        )
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        pot = Path(tmp) / f"{DOMAIN}.pot"
        _extract_pot(pot)
        _pybabel("update", "-i", str(pot), "-d", str(LOCALES_DIR))
    print(
        f"i18n-extract: OK — merged source strings into {len(_locale_dirs())} locale(s)"
    )
    return 0


def compile_catalogs() -> int:
    """Compile every ``.po`` into the committed ``.mo`` beside it."""
    locales = _locale_dirs()
    if not locales:
        print(f"i18n-compile: no locales under {LOCALES_DIR}.", file=sys.stderr)
        return 1
    _pybabel("compile", "-d", str(LOCALES_DIR))
    print(f"i18n-compile: OK — compiled {len(locales)} locale(s)")
    return 0


def check() -> int:
    """Fail when the committed catalogs differ from the sources."""
    locales = _locale_dirs()
    if not locales:
        print(f"i18n-check: no locales under {LOCALES_DIR}.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        pot = Path(tmp) / f"{DOMAIN}.pot"
        _extract_pot(pot)
        fresh = _message_ids(pot)

    problems: list[str] = []
    for locale_dir in locales:
        lang = locale_dir.name
        messages = locale_dir / "LC_MESSAGES"
        po = messages / f"{DOMAIN}.po"
        mo = messages / f"{DOMAIN}.mo"
        if not mo.is_file():
            problems.append(
                f"{lang}: missing compiled {mo.name} — run `just i18n-compile`"
            )
            continue
        committed = _message_ids(po)
        untracked = fresh - committed
        stale = committed - fresh
        if untracked:
            problems.append(
                f"{lang}: {len(untracked)} source string(s) missing from the "
                f"catalog — run `just i18n-extract` (e.g. "
                f"{sorted(map(str, untracked))[0]!r})"
            )
        if stale:
            problems.append(
                f"{lang}: {len(stale)} catalog entr(ies) no longer in the "
                f"source — run `just i18n-extract` (e.g. "
                f"{sorted(map(str, stale))[0]!r})"
            )
        if _compiled_bytes(po) != mo.read_bytes():
            problems.append(
                f"{lang}: committed {mo.name} does not match {po.name} — "
                "run `just i18n-compile`"
            )

    if problems:
        print(
            "i18n-check: the committed message catalogs are stale — they do "
            "not match the sources:\n\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        return 1

    print(
        "i18n-check: OK — committed catalogs match the source "
        f"({len(locales)} locale(s): {', '.join(d.name for d in locales)})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("extract", "compile", "check"))
    args = parser.parse_args(argv)
    if args.command == "extract":
        return extract()
    if args.command == "compile":
        return compile_catalogs()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
