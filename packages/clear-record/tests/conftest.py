"""Keep the test suite hermetic: **no real app state** and **English by default**.

Two leaks this closes:

- The i18n mechanism must leave the English default provably untouched: every
  existing test asserts exact English strings. A developer or CI machine whose
  ``LANG``/``LC_ALL`` names a shipped locale would otherwise pick up a translated
  console through ``i18n.resolve_locale`` and fail unrelated tests. ``CR_LANG=en``
  (the highest-precedence environment layer) pins the source strings, and the
  installed catalog is reset around every test.
- The app-owned directories (registry, config, cache, logs, models) are
  redirected to the test's ``tmp_path``, so a developer's real
  ``~/Library/Application Support/clear-record`` registry — or a config file —
  can never leak into a test or be written by one. Resolution goes through
  ``platformdirs`` (ADR-0025), whose platform-native paths ignore ``XDG_*`` on
  macOS, so the redirection replaces the resolved defaults directly rather than
  the environment.
- The **models** resolver also considers the old ``<cwd>/models`` default
  (adopting it only when it holds a ``ggml-*.bin``). A source checkout is exactly
  where a developer's real, gitignored ``models/`` cache lives, so the legacy
  candidate is aimed away from it: the suite replaces
  ``core.paths._cwd`` with a directory that cannot exist, and only a test that
  opts in points it at its own ``tmp_path``.

A test that wants a locale calls :func:`clear_record.core.i18n.install`/``use``
explicitly, or passes an explicit ``environ=`` to :func:`resolve_locale` — neither
depends on this environment.
"""

from __future__ import annotations

import pytest

from clear_record.core import i18n
from clear_record.core import paths


@pytest.fixture(autouse=True)
def _hermetic_english_environment(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CR_LANG", "en")
    # Redirect the platform-native bases; the legacy bases stay absent so the
    # ADR-0025 adoption path is only exercised where a test sets it up.
    app = tmp_path / "app"
    legacy = tmp_path / "legacy"
    monkeypatch.setattr(
        paths,
        "_defaults",
        paths.DefaultDirs(
            data=app / "data",
            config=app / "config",
            cache=app / "cache",
            state=app / "state",
            logs=app / "logs",
            legacy_data=legacy / "data",
            legacy_config=legacy / "config",
            legacy_cache=legacy / "cache",
            legacy_state=legacy / "state",
            legacy_logs=legacy / "logs",
        ),
    )
    monkeypatch.setattr(paths, "_notified", set())
    # Neutralise the legacy ``<cwd>/models`` candidate: see the module docstring.
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path / "no-such-cwd")
    i18n.reset()
    yield
    i18n.reset()
