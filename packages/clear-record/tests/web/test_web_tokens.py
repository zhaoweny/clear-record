"""Machine tokens over HTTP: the bearer gate, the console's list, and no destruction.

ADR-0033's machine-token half, driven through the **real app**. Three things are
proved here and nowhere else:

- a token satisfies the machine surface's half of the gate **without a cookie**,
  and only that half — the console's pages, fragments and its own mint/revoke
  routes refuse a token exactly as they refuse an anonymous request;
- the console mints and lists them (label, created, last-used, revoke), the
  plaintext appears **once**, and a revoke is effective on the very next request
  with no restart;
- the machine API carries no verb whose effect a durable copy cannot rebuild,
  proven **under token auth**: the table's DELETE routes are exactly the two the
  deletion contract names, and each of them holds when a token is the credential
  — the glossary delete retires (the row survives, and restores), and the tape
  delete refuses with nothing unlinked until a verified archive exists.
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from _console import signed_in
from clear_record.core import paths as core_paths
from clear_record.service import Registry
from clear_record.service.lifecycle import CONSOLE
from clear_record.web.app import create_app
from clear_record.web.auth import (
    MACHINE_PREFIX,
    SETUP_PATH,
    TOKENS_PATH,
    token_revoke_path,
)
from fastapi.testclient import TestClient

#: A client **on the node's machine** — the address a node binds — which is what
#: lets it name a path (a project's archive root; ADR-0032).
LOCAL_ORIGIN = "http://127.0.0.1:8765"

#: The plaintext of a minted token, read out of the response that showed it: the
#: one place it ever exists, which is what these tests are about.
_SECRET = re.compile(r'<code class="token-secret">([^<]+)</code>')

#: The console pages and fragments a token must never open.
CONSOLE_PATHS = ("/web/", "/web/settings/status", "/web/ui/projects")


@pytest.fixture(autouse=True)
def _managed_root(tmp_path, monkeypatch):
    """A temp managed root, so the upload/archive paths never touch real app state."""
    monkeypatch.setattr(core_paths, "config_path", lambda: tmp_path / "absent.toml")
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))


@pytest.fixture()
def console(tmp_path) -> SimpleNamespace:
    """One app: a signed-in **browser** client, and a **script** that holds only a token.

    The two clients are ``TestClient``s over the *same* app and registry, so the
    token the console mints is the token the script presents — and the script's
    cookie jar is empty, which is what makes "reaches the API without a session
    cookie" a real question rather than an accident of one jar.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    app = create_app(registry, trusted_hosts=("testserver",))
    return SimpleNamespace(
        app=app,
        registry=registry,
        browser=signed_in(TestClient(app, follow_redirects=False)),
        script=TestClient(app, base_url=LOCAL_ORIGIN, follow_redirects=False),
    )


def _mint(console: SimpleNamespace, label: str = "backup script") -> str:
    """Mint a token at the service seam and return its plaintext."""
    minted, _row = console.app.state.auth.mint_token(label, actor=CONSOLE)
    return minted


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _shown(response) -> str:
    """The plaintext a mint response showed, or the failure that it showed none."""
    match = _SECRET.search(response.text)
    assert match is not None, "the mint response did not show the plaintext once"
    return match.group(1)


# --- the bearer branch of the gate ------------------------------------------- #


def test_a_token_reaches_the_machine_api_without_a_session_cookie(console) -> None:
    """The script holds a header, no cookie, and the API answers it."""
    token = _mint(console)

    assert not console.script.cookies, "the script's jar is empty on purpose"
    refused = console.script.get(f"{MACHINE_PREFIX}projects")
    assert refused.status_code == 401
    # The refusal names both ways in, so a client that is refused knows what
    # would have worked rather than having to read the docs.
    assert "Bearer" in refused.json()["detail"]
    assert SETUP_PATH in refused.json()["detail"]

    allowed = console.script.get(f"{MACHINE_PREFIX}projects", headers=_bearer(token))

    assert allowed.status_code == 200
    assert "set-cookie" not in allowed.headers, "a token request issues no cookie"


def test_the_bearer_grammar_is_one_shape_with_a_case_insensitive_scheme(
    console,
) -> None:
    """Only ``Authorization: Bearer <token>`` is a credential; nothing else is guessed at."""
    token = _mint(console)

    for value in (f"Bearer {token}", f"bearer {token}", f"Bearer   {token}"):
        response = console.script.get(
            f"{MACHINE_PREFIX}projects", headers={"Authorization": value}
        )
        assert response.status_code == 200, value

    for value in (
        token,
        f"Basic {token}",
        f"Bearer {token} extra",
        "Bearer",
        "",
    ):
        response = console.script.get(
            f"{MACHINE_PREFIX}projects", headers={"Authorization": value}
        )
        assert response.status_code == 401, value

    other_header = console.script.get(
        f"{MACHINE_PREFIX}projects", headers={"X-Api-Token": token}
    )
    assert other_header.status_code == 401, "a token arrived under another header"


def test_a_revoked_token_is_refused_on_the_next_request_with_no_restart(
    console,
) -> None:
    """Revoking deletes the row the gate reads per request: the next call is out.

    Nothing is restarted between the two calls — the same app object answers both
    — because there is no state to expire: the gate looks the row up on every
    request, which is what makes a revoke immediate rather than eventual.
    """
    token = _mint(console, "ci")
    assert (
        console.script.get(
            f"{MACHINE_PREFIX}projects", headers=_bearer(token)
        ).status_code
        == 200
    )
    row = console.registry.machine_tokens()[0]

    revoked = console.browser.post(token_revoke_path(row.id))

    assert revoked.status_code == 200
    assert console.registry.machine_tokens() == []
    assert (
        console.script.get(
            f"{MACHINE_PREFIX}projects", headers=_bearer(token)
        ).status_code
        == 401
    )


def test_a_token_never_opens_the_console_and_cannot_mint_or_revoke(console) -> None:
    """The token's reach is the machine surface: pages, fragments and the token routes refuse it.

    This is the whole of the "must not widen the console's browser surface" rule,
    asserted on the surfaces a token could otherwise be hoped to open — including
    the two routes that mint and revoke tokens, so a token can never grow its own
    kind.
    """
    token = _mint(console, "ci")

    for path in CONSOLE_PATHS:
        response = console.script.get(path, headers=_bearer(token))
        assert response.status_code == 303, path
        assert response.headers["location"] == SETUP_PATH, path

    fragment = console.script.get(
        "/web/ui/projects",
        headers={**_bearer(token), "HX-Request": "true"},
    )
    assert fragment.status_code == 200
    assert fragment.headers["HX-Redirect"] == SETUP_PATH

    for method, path, data in (
        ("POST", TOKENS_PATH, {"label": "self"}),
        ("POST", token_revoke_path(1), None),
    ):
        response = console.script.request(
            method, path, data=data, headers=_bearer(token)
        )
        assert response.status_code == 303, (method, path)
        assert response.headers["location"] == SETUP_PATH

    assert [t.label for t in console.registry.machine_tokens()] == ["ci"], (
        "a token minted or revoked its own kind"
    )


def test_a_token_request_does_not_stall_the_node_while_another_writer_holds_it(
    console,
) -> None:
    """The lazy timestamp keeps a busy script from blocking everything else.

    Led by measurement: with another process holding the registry's write lock, a
    timestamp written on **every** request waits out the driver's busy timeout
    *inside* the async middleware, and every other request the node is serving
    stalls behind it — an unrelated anonymous ``/health`` probe measured **3.5 s**
    while a token request measured **5.0 s**. The write is lazy now, so a request
    whose token is inside the window performs no write at all and answers at once.

    The bound is deliberately loose: a real answer is milliseconds and a blocked
    one is the driver's whole busy timeout (seconds), so anything under two
    seconds proves no write was attempted.
    """
    token = _mint(console, "ci")
    # One use, so the token's stamp is fresh: the requests below are inside the
    # window and must therefore perform no write.
    assert (
        console.script.get(
            f"{MACHINE_PREFIX}projects", headers=_bearer(token)
        ).status_code
        == 200
    )

    with closing(sqlite3.connect(str(console.registry.db_path))) as holder, holder:
        holder.execute("BEGIN IMMEDIATE")  # another writer holds the registry

        started = time.monotonic()
        answered = console.script.get(
            f"{MACHINE_PREFIX}projects", headers=_bearer(token)
        )
        token_wait = time.monotonic() - started

        started = time.monotonic()
        probe = console.script.get("/health")  # anonymous, and unrelated
        probe_wait = time.monotonic() - started

    assert answered.status_code == 200, "a locked registry is not a refusal"
    assert probe.status_code == 200
    assert token_wait < 2.0, f"the token request waited {token_wait:.2f}s on a write"
    assert probe_wait < 2.0, f"the probe waited {probe_wait:.2f}s behind the gate"


# --- the console's list ------------------------------------------------------ #


def test_minting_shows_the_plaintext_once_and_never_again(console) -> None:
    """The one-time secret lives in the mint response; no later request rebuilds it."""
    minted = console.browser.post(TOKENS_PATH, data={"label": "laptop"})
    secret = _shown(minted)

    assert minted.status_code == 200
    assert secret.encode() not in console.registry.db_path.read_bytes()

    # A reload is a GET, and a GET renders the registry's rows — a digest, never
    # a token — so neither the value nor the one-time block comes back.
    page = console.browser.get("/web/settings/status")
    assert secret not in page.text
    assert 'class="token-secret"' not in page.text
    assert "laptop" in page.text, "the row itself is still listed"

    # And a second mint shows its own secret, not the first one's.
    second = console.browser.post(TOKENS_PATH, data={"label": "desktop"})
    assert _shown(second) != secret
    assert secret not in second.text


def test_the_list_shows_label_created_last_used_and_the_revoke_action(console) -> None:
    """Everything the operator judges a token by, and the action that ends it."""
    minted = console.browser.post(TOKENS_PATH, data={"label": "backup script"})
    secret = _shown(minted)
    row = console.registry.machine_tokens()[0]

    assert "backup script" in minted.text
    assert row.created_at in minted.text
    assert ">never<" in minted.text, "an unused token says so"
    assert f'hx-post="{token_revoke_path(row.id)}"' in minted.text

    # The use the list is about: the script presents it, and the next render
    # shows when.
    console.script.get(f"{MACHINE_PREFIX}projects", headers=_bearer(secret))
    used = console.registry.machine_tokens()[0].last_used_at
    assert used is not None
    page = console.browser.get("/web/settings/status")
    assert used in page.text

    # The action the row carries ends it, and the list loses the row.
    assert console.browser.post(token_revoke_path(row.id)).status_code == 200
    assert "backup script" not in console.browser.get("/web/settings/status").text
    assert (
        console.script.get(
            f"{MACHINE_PREFIX}projects", headers=_bearer(secret)
        ).status_code
        == 401
    )


def test_a_refused_mint_re_renders_with_the_service_message(console) -> None:
    """A bad label is a form mistake, not a 4xx htmx would swallow."""
    assert (
        console.browser.post(TOKENS_PATH, data={"label": "laptop"}).status_code == 200
    )

    duplicate = console.browser.post(TOKENS_PATH, data={"label": "laptop"})
    assert duplicate.status_code == 200
    assert "already exists" in duplicate.text
    assert 'class="token-secret"' not in duplicate.text

    blank = console.browser.post(TOKENS_PATH, data={"label": "   "})
    assert blank.status_code == 200
    assert "needs a label" in blank.text

    assert [t.label for t in console.registry.machine_tokens()] == ["laptop"]


# --- the deletion contract, under token auth --------------------------------- #


def _machine_deletes(console: SimpleNamespace) -> set[str]:
    """Every route under the machine prefix that answers ``DELETE``."""
    return {
        str(route.path)
        for route in console.app.routes
        if "DELETE" in (getattr(route, "methods", None) or set())
        and str(route.path).startswith(MACHINE_PREFIX)
    }


def test_the_machine_api_has_exactly_two_delete_verbs_and_they_are_the_named_ones(
    console,
) -> None:
    """No route can join the destructive shape without failing here.

    The table is the guard: the machine surface's two ``DELETE`` routes are the
    glossary *retire* (the row survives the verb) and the managed-tape delete
    (licensed by a verified archive). A third one — the shape a destructive verb
    arrives in — fails this test before anything is built on it, which is the
    decision point ADR-0033 asks for.
    """
    assert _machine_deletes(console) == {
        f"{MACHINE_PREFIX}glossary/{{term_id}}",
        f"{MACHINE_PREFIX}meetings/{{meeting_id}}/tapes/{{tape_id}}",
    }


def test_a_token_can_retire_a_term_and_the_row_survives(console) -> None:
    """The glossary ``DELETE`` is a retire: the row keeps its history and restores.

    Everything here is done **by the token**, from a client with no cookie — the
    verb's effect is what is under test, and it is reconstructible from the
    registry's own row rather than from a backup.
    """
    token = _mint(console, "script")
    auth = _bearer(token)
    console.script.post(f"{MACHINE_PREFIX}projects", json={"name": "Ops"}, headers=auth)
    term = console.script.post(
        f"{MACHINE_PREFIX}projects/ops/glossary",
        json={"term": "Alpha"},
        headers=auth,
    ).json()

    retired = console.script.delete(
        f"{MACHINE_PREFIX}glossary/{term['id']}", headers=auth
    )

    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"
    listed = console.script.get(
        f"{MACHINE_PREFIX}projects/ops/glossary", headers=auth
    ).json()
    assert [row["id"] for row in listed] == [term["id"]], "the row was not deleted"

    restored = console.script.post(
        f"{MACHINE_PREFIX}glossary/{term['id']}/restore", headers=auth
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "candidate"


def test_a_token_cannot_delete_a_tape_without_a_durable_copy(console) -> None:
    """The tape delete refuses until a verified archive exists, and then names it.

    The refusal is the contract: nothing is unlinked, and the sentence names the
    action that would make the delete reconstructible. Once that archive exists —
    created through the same token — the delete lands and the archive it leaned on
    is still there, which is what "reconstructible from a durable copy" means for
    the one verb on this surface that removes bytes.
    """
    token = _mint(console, "script")
    auth = _bearer(token)
    archive_root = console.registry.db_path.parent / "archive"
    console.script.post(
        f"{MACHINE_PREFIX}projects",
        json={"name": "Ops", "default_archive_root": str(archive_root)},
        headers=auth,
    )
    meeting = console.script.post(
        f"{MACHINE_PREFIX}projects/ops/meetings",
        json={"title": "Kickoff", "managed": True},
        headers=auth,
    ).json()
    tape = console.script.post(
        f"{MACHINE_PREFIX}meetings/{meeting['id']}/tapes",
        files={"file": ("a.wav", b"x", "audio/wav")},
        headers=auth,
    ).json()
    tape_url = f"{MACHINE_PREFIX}meetings/{meeting['id']}/tapes/{tape['id']}"

    refused = console.script.delete(tape_url, headers=auth)
    assert refused.status_code == 400
    assert "archive" in refused.json()["detail"]
    assert Path(tape["path"]).exists(), "a refused delete unlinks nothing"

    archive = console.script.post(
        f"{MACHINE_PREFIX}meetings/{meeting['id']}/archives", json={}, headers=auth
    ).json()

    deleted = console.script.delete(tape_url, headers=auth)
    assert deleted.status_code == 200
    assert archive["root_path"] in deleted.json()["note"]
    assert not Path(tape["path"]).exists()
    assert Path(archive["manifest_path"]).exists(), "the durable copy survived"
