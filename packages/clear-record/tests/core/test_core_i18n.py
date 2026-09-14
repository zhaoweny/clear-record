"""The i18n lookup: English by default, a catalog when installed.

Runtime is stdlib ``gettext`` (``clear_record.core`` must stay third-party-free),
so these tests install catalogs through the public API and never touch Babel.
The suite-wide ``conftest`` already pins ``CR_LANG=en`` and resets the catalog;
the tests here install explicitly.
"""

from __future__ import annotations

import gettext

from clear_record.core import i18n


class _Pseudo(gettext.NullTranslations):
    """A transforming catalog: proves ``tr``/``trn`` are consulted, not assumed."""

    def gettext(self, message: str) -> str:  # type: ignore[override]
        return f"«{message}»"

    def ngettext(self, singular: str, plural: str, n: int) -> str:  # type: ignore[override]
        return f"«{singular if n == 1 else plural}»"


def test_english_source_is_verbatim_with_no_catalog() -> None:
    """No catalog installed → the English message ID, interpolated."""
    i18n.reset()
    assert i18n.tr("Delete term") == "Delete term"
    assert i18n.tr("Deleted {name}", name="Falcon") == "Deleted Falcon"
    assert i18n.trn("{count} file", "{count} files", 1, count=1) == "1 file"
    assert i18n.trn("{count} file", "{count} files", 2, count=2) == "2 files"


def test_resolve_locale_precedence_and_normalization() -> None:
    """``explicit`` > ``CR_LANG`` > ``LC_ALL`` > ``LANG`` > English."""
    assert (
        i18n.resolve_locale("it", environ={"CR_LANG": "de", "LC_ALL": "fr_FR.UTF-8"})
        == "it"
    )
    assert (
        i18n.resolve_locale(
            environ={"CR_LANG": "de", "LC_ALL": "fr_FR.UTF-8", "LANG": "es"}
        )
        == "de"
    )
    assert (
        i18n.resolve_locale(environ={"LC_ALL": "fr_FR.UTF-8", "LANG": "es"}) == "fr_FR"
    )
    assert i18n.resolve_locale(environ={"LANG": "de_DE.UTF-8@euro"}) == "de_DE"
    assert i18n.resolve_locale(environ={}) == "en"


def test_resolve_locale_treats_c_and_posix_as_no_preference() -> None:
    """``LANG=C`` means "no translation", so the next candidate wins."""
    assert i18n.resolve_locale(environ={"CR_LANG": "C", "LANG": "de"}) == "de"
    assert i18n.resolve_locale(environ={"LANG": "POSIX"}) == "en"


def test_unknown_locale_falls_back_to_english() -> None:
    """A locale with no compiled catalog is not an error."""
    i18n.install("zz")
    assert i18n.tr("Add project") == "Add project"


def test_shipped_german_catalog_translates_and_falls_back() -> None:
    """The one shipped catalog proves the mechanism end to end."""
    assert "de" in i18n.available_locales()
    i18n.install("de")
    assert i18n.tr("Add project") == "Projekt hinzufügen"
    # An untranslated ID stays the English source (incremental adoption).
    assert i18n.tr("Not in any catalog.") == "Not in any catalog."


def test_german_plural_uses_the_catalog_rule() -> None:
    i18n.install("de")
    assert (
        i18n.trn("{count} file verified", "{count} files verified", 1, count=1)
        == "1 Datei verifiziert"
    )
    assert (
        i18n.trn("{count} file verified", "{count} files verified", 3, count=3)
        == "3 Dateien verifiziert"
    )


def test_use_installs_a_transforming_catalog() -> None:
    i18n.use(_Pseudo())
    assert i18n.tr("hi") == "«hi»"
    assert i18n.tr("Hi {n}", n=1) == "«Hi 1»"
    assert i18n.trn("one", "many", 2) == "«many»"


def test_deferred_marks_without_translating() -> None:
    """``deferred`` only marks an ID for extraction; it returns English."""
    i18n.use(_Pseudo())
    assert i18n.deferred("clear-record: ...") == "clear-record: ..."


def test_install_if_unset_does_not_override_an_explicit_choice() -> None:
    i18n.install("de")
    i18n.install_if_unset("fr", environ={})
    assert i18n.current_locale() == "de"


def test_install_if_unset_honours_the_environment_when_unset() -> None:
    i18n.reset()
    i18n.install_if_unset(environ={"CR_LANG": "de"})
    assert i18n.current_locale() == "de"
