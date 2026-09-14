"""Keep the test suite hermetic: **no real app state** and **English by default**.

Two leaks this closes:

- The i18n mechanism must leave the English default provably untouched: every
  existing test asserts exact English strings. A developer or CI machine whose
  ``LANG``/``LC_ALL`` names a shipped locale would otherwise pick up a translated
  console through ``i18n.resolve_locale`` and fail unrelated tests. ``CR_LANG=en``
  (the highest-precedence environment layer) pins the source strings, and the
  installed catalog is reset around every test.
- The app-owned XDG directories (registry, config, logs) are redirected to the
  test's ``tmp_path``, so a developer's real ``~/.local/share/clear-record``
  registry — or a config file — can never leak into a test or be written by one.

A test that wants a locale calls :func:`clear_record.core.i18n.install`/``use``
explicitly, or passes an explicit ``environ=`` to :func:`resolve_locale` — neither
depends on this environment.
"""

from __future__ import annotations

import pytest

from clear_record.core import i18n


@pytest.fixture(autouse=True)
def _hermetic_english_environment(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CR_LANG", "en")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    i18n.reset()
    yield
    i18n.reset()
