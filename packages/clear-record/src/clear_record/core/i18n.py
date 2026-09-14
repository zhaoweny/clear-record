"""Translation lookup for user-facing UI strings (stdlib only).

The owner asked for "some i18n ... a ``tr()`` shorthand for strings needed to
translate" (2026-09-15, verbatim). This module is the one lookup point:

- :func:`tr` and :func:`trn` are the shorthand. **Message IDs are the English
  source strings**, so English needs no catalog and adopting i18n is incremental
  — an untranslated string still renders correctly.
- The runtime is **stdlib :mod:`gettext`**: ``core`` stays free of third-party
  code (the layering guard, ADR-0012) and no runtime dependency is added.
  ``Babel`` (``pybabel``) is a **build-time tool only** (``just i18n-extract`` /
  ``i18n-compile`` / ``i18n-check``); nothing here imports it.
- With **no catalog installed, ``tr`` returns the English source string
  verbatim**. That is what keeps every existing test and the byte-identical CLI
  default unchanged.

The compiled catalogs live beside this package as
``clear_record/locales/<lang>/LC_MESSAGES/messages.mo`` (source ``.po`` next to
it; the wheel carries both as package data). The compiled output is committed
and a freshness guard (``just i18n-check``, its own CI job) fails when it drifts
from the source — the same discipline as the console's compiled front-end
assets (ADR-0023).

What is **never** translated (a rule, not a preference): JSONL log records, the
JSON API, export artifacts and the diagnostics bundle. Those are machine-read
or the user's own data; a translated log line would break parsing. See
``docs/i18n.md``.
"""

from __future__ import annotations

import gettext as _gettext
import os
from collections.abc import Mapping
from pathlib import Path

#: The catalog directory, shipping inside the import package:
#: ``clear_record/locales``. ``uv_build`` carries it as package data.
LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
#: The gettext domain — the catalog basename (``messages.po`` / ``messages.mo``).
DOMAIN = "messages"
#: ``CR_LANG`` overrides the environment's locale (ADR-0007's ``CR_*`` shape).
ENV_LANG = "CR_LANG"
#: The source locale: no catalog is consulted and the English string is returned.
SOURCE_LOCALE = "en"

#: The installed translations. A :class:`gettext.NullTranslations` returns the
#: message ID unchanged, which *is* the English default.
_translations: _gettext.NullTranslations = _gettext.NullTranslations()
#: Whether :func:`install` / :func:`use` has been called (so a later
#: :func:`install_if_unset` will not override an explicit choice).
_installed = False
#: The locale installed (or ``None`` for the English/null default).
_locale: str | None = None


# --------------------------------------------------------------------------- #
# the shorthand
# --------------------------------------------------------------------------- #
def tr(msgid: str, **vars: object) -> str:
    """Translate one English message ID, then interpolate ``{name}`` placeholders.

    ``tr("Deleted {name}", name=…)``. With no catalog installed the message ID
    comes back verbatim (interpolated), so this is safe to adopt incrementally.
    """
    message = _translations.gettext(msgid)
    return message.format(**vars) if vars else message


def trn(singular: str, plural: str, n: int, /, **vars: object) -> str:
    """Translate and choose a plural form, then interpolate placeholders.

    ``trn("{count} tape", "{count} tapes", count, count=count)``. English picks
    by ``n`` (``== 1`` → singular); a catalog supplies the locale's plural rule
    via :func:`gettext.ngettext`. ``n`` is positional-only so a placeholder may
    also be named ``n``.
    """
    message = _translations.ngettext(singular, plural, n)
    return message.format(**vars) if vars else message


def deferred(msgid: str) -> str:
    """Return an English message ID unchanged, **marking it for extraction**.

    For a string whose lookup happens later than module import — the Click help
    assembled after ``--lang`` is parsed — the extraction tool must still see a
    literal call. ``deferred`` is that marker: it translates nothing now, and
    ``tr`` looks the ID up at build time.
    """
    return msgid


# --------------------------------------------------------------------------- #
# installing a catalog
# --------------------------------------------------------------------------- #
def normalize_locale(value: str | None) -> str | None:
    """Reduce a locale string to a language tag, or ``None`` for "no preference".

    ``fr_FR.UTF-8@euro`` → ``fr_FR``; ``de`` → ``de``. ``C``/``POSIX`` (and an
    empty value) mean "no translation", signalled by ``None`` so the caller can
    fall through to the next candidate.
    """
    if not value:
        return None
    code = value.split(".")[0].split("@")[0].strip()
    if not code or code in {"C", "POSIX"}:
        return None
    return code


def resolve_locale(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The locale to use, by the ticket's precedence.

    ``explicit`` (the CLI's ``--lang``) > ``CR_LANG`` > ``LC_ALL`` > ``LANG`` >
    English. The config-file layer and the web's ``Accept-Language``/cookie are
    **deferred** (see ``docs/i18n.md``): ``core`` may not read the service layer's
    config file, and the console has no per-request locale yet.

    ``environ`` is injectable so a test can assert the order without touching the
    process environment.
    """
    env = os.environ if environ is None else environ
    for candidate in (
        explicit,
        env.get(ENV_LANG),
        env.get("LC_ALL"),
        env.get("LANG"),
    ):
        code = normalize_locale(candidate)
        if code:
            return code
    return SOURCE_LOCALE


def install(locale: str | None = None, *, localedir: str | Path | None = None) -> None:
    """Install the catalog for ``locale`` (``None``/``en`` → the English default).

    A locale whose compiled catalog is absent falls back to English rather than
    raising — an incomplete translation is expected, since message IDs are the
    English strings.
    """
    global _translations, _installed, _locale
    code = normalize_locale(locale)
    if code is None or code == SOURCE_LOCALE:
        _translations = _gettext.NullTranslations()
        _locale = None
        _installed = True
        return
    _translations = _gettext.translation(
        DOMAIN,
        localedir=str(LOCALES_DIR if localedir is None else localedir),
        languages=[code],
        fallback=True,
    )
    _locale = code
    _installed = True


def install_if_unset(
    locale: str | None = None, *, environ: Mapping[str, str] | None = None
) -> None:
    """Install only when nothing has installed a catalog yet.

    Lets a surface that is entered directly (the tray, a programmatic
    ``create_app``) honour the environment, while a **CLI ``--lang`` already
    installed wins** — it is not overridden by a later environment read.
    """
    if _installed:
        return
    install(locale if locale is not None else resolve_locale(environ=environ))


def use(translations: _gettext.NullTranslations) -> None:
    """Install an explicit translations object (tests, embedders).

    The class is :class:`gettext.NullTranslations`, so a **transforming
    pseudo-catalog** is a subclass that overrides :meth:`gettext` /
    :meth:`ngettext`; a real catalog is what :func:`install` builds.
    """
    global _translations, _installed, _locale
    _translations = translations
    _installed = True
    _locale = None


def reset() -> None:
    """Restore the English default and forget the installed locale."""
    global _translations, _installed, _locale
    _translations = _gettext.NullTranslations()
    _installed = False
    _locale = None


def current_locale() -> str | None:
    """The installed locale, or ``None`` for the English/null default."""
    return _locale


def available_locales() -> list[str]:
    """Locales with a compiled catalog, sorted (the shipped translations)."""
    if not LOCALES_DIR.is_dir():
        return []
    return sorted(
        child.name
        for child in LOCALES_DIR.iterdir()
        if (child / "LC_MESSAGES" / f"{DOMAIN}.mo").is_file()
    )


__all__ = [
    "DOMAIN",
    "ENV_LANG",
    "LOCALES_DIR",
    "SOURCE_LOCALE",
    "available_locales",
    "current_locale",
    "deferred",
    "install",
    "install_if_unset",
    "normalize_locale",
    "reset",
    "resolve_locale",
    "tr",
    "trn",
    "use",
]
