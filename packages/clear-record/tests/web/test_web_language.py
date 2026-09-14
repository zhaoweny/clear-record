"""The console's language switcher and per-request locale precedence.

The web chain is **cookie > ``Accept-Language`` > ``CR_LANG`` > environment >
English** (the CLI chain is separate: ``--lang`` > ``CR_LANG`` > ``LC_ALL`` >
``LANG``). The cookie is the only new state and carries a language tag and
nothing else.

English is the default and stays byte-identical: with no cookie and no
``Accept-Language`` (which is what the test client sends) the source strings
render. These tests exercise the whole chain through the real app, plus the
negotiation helper directly with an injected environment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clear_record.service import Registry
from clear_record.web.app import LANG_COOKIE, create_app, resolve_web_locale


@pytest.fixture()
def client(tmp_path) -> TestClient:
    app = create_app(Registry.open(db_path=tmp_path / "registry.sqlite3"))
    return TestClient(app)


# --- the switcher lives in the console, legible in both locales ------------- #
def test_english_is_default_and_the_switcher_lists_both_locales(client) -> None:
    home = client.get("/")
    assert 'lang="en"' in home.text
    assert "New project name" in home.text  # English source string
    # Both endonyms are present whatever the locale — a picker you cannot read
    # is a bug — and the current choice is marked.
    assert "English" in home.text
    assert "简体中文" in home.text
    assert 'aria-current="true">English' in home.text
    assert 'aria-current="true">简体中文' not in home.text


def test_switcher_is_labelled_in_the_active_locale(client) -> None:
    english = client.get("/").text
    assert "Language" in english  # the control's own label, in English

    client.cookies.set(LANG_COOKIE, "zh_CN")
    chinese = client.get("/").text
    assert "语言" in chinese  # the same label, translated
    assert 'aria-current="true">简体中文' in chinese


# --- the selection chain ---------------------------------------------------- #
def test_accept_language_selects_a_shipped_catalog(client) -> None:
    home = client.get("/", headers={"accept-language": "zh-CN,zh;q=0.9,en;q=0.8"})
    assert 'lang="zh-CN"' in home.text
    assert "添加项目" in home.text
    assert 'aria-current="true">简体中文' in home.text


def test_cookie_overrides_accept_language(client) -> None:
    client.cookies.set(LANG_COOKIE, "zh_CN")
    home = client.get("/", headers={"accept-language": "en-US,en;q=0.9"})
    assert "添加项目" in home.text


def test_accept_language_overrides_cr_lang(client, monkeypatch) -> None:
    monkeypatch.setenv("CR_LANG", "en")
    home = client.get("/", headers={"accept-language": "zh-CN"})
    assert "添加项目" in home.text

    monkeypatch.setenv("CR_LANG", "zh_CN")
    home = client.get("/", headers={"accept-language": "en-US"})
    assert "New project name" in home.text


def test_cr_lang_applies_when_no_cookie_or_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_LANG", "zh_CN")
    app = create_app(Registry.open(db_path=tmp_path / "r.sqlite3"))
    home = TestClient(app).get("/")
    assert "添加项目" in home.text


def test_unsupported_preference_falls_back_to_english(client) -> None:
    home = client.get("/", headers={"accept-language": "fr-FR,fr;q=0.9"})
    assert "New project name" in home.text
    assert 'lang="en"' in home.text


# --- the cookie ------------------------------------------------------------- #
def test_switcher_persists_only_the_language_cookie(tmp_path) -> None:
    app = create_app(Registry.open(db_path=tmp_path / "r.sqlite3"))
    client = TestClient(app, follow_redirects=False)
    response = client.post("/ui/language", data={"lang": "zh_CN"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{LANG_COOKIE}=zh_CN")
    # The value is the tag and nothing else: no session, no identity.
    assert response.cookies[LANG_COOKIE] == "zh_CN"
    assert len(response.cookies) == 1

    followed = client.get("/")
    assert "添加项目" in followed.text


def test_switcher_ignores_an_unknown_value(tmp_path) -> None:
    app = create_app(Registry.open(db_path=tmp_path / "r.sqlite3"))
    client = TestClient(app, follow_redirects=False)
    response = client.post("/ui/language", data={"lang": "xx"})
    assert response.status_code == 303
    assert "set-cookie" not in response.headers


# --- boundaries ------------------------------------------------------------- #
def test_json_api_stays_english_under_a_chinese_console(client) -> None:
    client.cookies.set(LANG_COOKIE, "zh_CN")
    assert "添加项目" in client.get("/").text
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.post("/api/projects", json={"name": "Ops"}).status_code == 201
    term = client.post("/api/projects/ops/glossary", json={"term": "Falcon"}).json()
    assert term["status"] == "candidate"


# --- the helper, with an injected environment ------------------------------- #
def test_resolve_web_locale_precedence() -> None:
    environ = {"CR_LANG": "zh_CN", "LANG": "en_US.UTF-8"}
    assert resolve_web_locale("zh_CN", "en-US", environ=environ) == "zh_CN"
    assert resolve_web_locale(None, "en-US,en;q=0.9", environ=environ) == "en"
    assert resolve_web_locale(None, "zh-CN,zh;q=0.9", environ={}) == "zh_CN"
    assert resolve_web_locale(None, "zh", environ={}) == "zh_CN"
    # An unsupported preference is skipped, never guessed: the environment tail
    # decides, and English is the last resort.
    assert resolve_web_locale(None, "fr-FR", environ=environ) == "zh_CN"
    assert resolve_web_locale(None, None, environ=environ) == "zh_CN"
    assert resolve_web_locale(None, None, environ={"LANG": "zh_CN.UTF-8"}) == "zh_CN"
    assert resolve_web_locale(None, None, environ={}) == "en"
