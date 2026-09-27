"""The machine tokens a script carries, at the service seam (ADR-0033).

The credential a *script* presents to the machine API, driven directly — no HTTP:
minting shows the plaintext once and stores only its digest, a label names one
token, a use moves the last-used instant, and a revoke is a delete that takes
effect immediately. The two attribution questions — who minted a token, who
revoked it — are audit rows; a token's own use is bookkeeping and appends none.

The HTTP half is ``tests/web/test_web_tokens.py``: the bearer branch of the gate,
the console's list, and the deletion contract under token auth.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy.exc import OperationalError

import clear_record.service.auth as auth_module
from clear_record.service.auth import (
    TOKEN_LABEL_MAX_LENGTH,
    ConsoleAuth,
    SessionState,
    new_machine_token,
    require_token_label,
    token_digest,
)
from clear_record.service.lifecycle import CONSOLE
from clear_record.service.store import Registry


def _console(tmp_path) -> ConsoleAuth:
    """A seam over a temp registry, with a credential already set."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    console = ConsoleAuth(registry)
    console.set_password("service-seam-password-91ab", actor=CONSOLE)
    return console


# --- minting ----------------------------------------------------------------- #


def test_a_minted_token_is_shown_once_and_stored_only_as_its_digest(tmp_path) -> None:
    """The plaintext leaves the seam; the registry holds a digest and nothing else."""
    console = _console(tmp_path)

    minted, row = console.mint_token("backup script", actor=CONSOLE)

    assert row.label == "backup script"
    assert row.last_used_at is None, "a fresh token has never been presented"
    assert minted != token_digest(minted), "the plaintext is not what is listed"
    raw = console.registry.db_path.read_bytes()
    assert minted.encode() not in raw
    assert token_digest(minted).encode() in raw, "the digest is the row's key"

    # The boundary value deliberately has no field for the secret, so no
    # template or JSON response can print it from the list.
    assert not hasattr(row, "token_digest")
    assert [t.label for t in console.machine_tokens()] == ["backup script"]


def test_a_minted_token_authenticates_and_a_use_moves_its_last_used(tmp_path) -> None:
    console = _console(tmp_path)
    minted, row = console.mint_token("ci", actor=CONSOLE)

    seen = console.authenticate_token(minted)

    assert seen is not None and seen.id == row.id
    # The row is re-read: ``authenticate_token`` answers with the row as it was
    # found and then moves the instant, so the use is visible on the next read —
    # which is exactly the read the console's list makes.
    assert console.machine_tokens()[0].last_used_at is not None


def test_an_unknown_or_absent_token_is_refused_and_nothing_is_touched(
    tmp_path,
) -> None:
    console = _console(tmp_path)
    console.mint_token("ci", actor=CONSOLE)

    for absent in (None, "", "a-token-this-registry-never-minted"):
        assert console.authenticate_token(absent) is None, absent

    assert console.machine_tokens()[0].last_used_at is None, "a refusal is not a use"


def test_a_use_is_recorded_lazily_and_a_burst_writes_nothing(
    tmp_path, monkeypatch
) -> None:
    """A token's use moves its stamp at most once per window, not once per request.

    The gate runs inside an ``async`` middleware and the write is synchronous, so
    a commit per request would block the event loop for every call a script makes
    — and a registry another writer holds for a moment would then stall the whole
    node, not just that request. The decision is taken from the row the look-up
    already read, so a request that is not due performs **no write at all**.
    """
    now = [dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)]
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    console = ConsoleAuth(registry, clock=lambda: now[0])
    console.set_password("service-seam-password-91ab", actor=CONSOLE)
    minted, _row = console.mint_token("ci", actor=CONSOLE)
    writes: list[dt.datetime] = []
    real = registry.touch_machine_token

    def counted(digest: str, *, used_at: dt.datetime) -> None:
        writes.append(used_at)
        real(digest, used_at=used_at)

    monkeypatch.setattr(registry, "touch_machine_token", counted)

    assert console.authenticate_token(minted) is not None
    assert len(writes) == 1, (
        "a token's first use is what the list cannot otherwise show"
    )

    for _ in range(5):
        assert console.authenticate_token(minted) is not None
    assert len(writes) == 1, "a burst inside the window wrote the clock again"
    assert console.machine_tokens()[0].last_used_at is not None

    # Read off the module rather than imported by name: the window is the seam's
    # own declaration, and a test that hard-coded a duration would drift from it.
    now[0] += auth_module.TOKEN_TOUCH_INTERVAL
    assert console.authenticate_token(minted) is not None
    assert len(writes) == 2, "a use past the window was not recorded"


def test_a_use_whose_timestamp_cannot_be_written_still_authenticates(
    tmp_path, monkeypatch
) -> None:
    """The gate's own lesson: a write that meets a lock costs the write, not the request.

    The last-used update is a write per authenticated request, and another
    surface can hold the registry while it runs. Raising there would answer a
    correctly authenticated request with a 500 — the one thing the gate must
    never do — so the token is returned and the timestamp is what waits for the
    next request.
    """
    console = _console(tmp_path)
    minted, row = console.mint_token("ci", actor=CONSOLE)

    def locked(_digest: str, *, used_at: dt.datetime) -> None:
        raise OperationalError(
            "UPDATE machine_token SET last_used_at = ?",
            {},
            Exception("database is locked"),
        )

    monkeypatch.setattr(console.registry, "touch_machine_token", locked)

    assert console.authenticate_token(minted) is not None
    assert console.machine_tokens()[0].last_used_at is None, "the write really failed"


@pytest.mark.parametrize(
    "label",
    [
        "",
        "   ",
        "\n",
        "two\nlines",
        # A no-break space reads as a blank and is a different character: the rule
        # is "printable", not "not a control character" (`str.isprintable`).
        "a\u00a0b",
        "x" * (TOKEN_LABEL_MAX_LENGTH + 1),
    ],
)
def test_a_label_the_rule_refuses_is_never_stored(tmp_path, label) -> None:
    console = _console(tmp_path)

    with pytest.raises(ValueError):
        console.mint_token(label, actor=CONSOLE)

    assert console.machine_tokens() == []


def test_a_label_is_trimmed_and_the_rule_is_one_statement() -> None:
    assert require_token_label("  laptop  ") == "laptop"
    assert require_token_label("x" * TOKEN_LABEL_MAX_LENGTH)

    with pytest.raises(ValueError, match="needs a label"):
        require_token_label("  ")
    with pytest.raises(ValueError, match="at most"):
        require_token_label("x" * (TOKEN_LABEL_MAX_LENGTH + 1))
    with pytest.raises(ValueError, match="printable characters"):
        require_token_label("a\tb")
    with pytest.raises(ValueError, match="printable characters"):
        require_token_label("a\u3000b")


def test_a_label_names_one_token(tmp_path) -> None:
    """A second mint under a label already in use is refused, naming it.

    The label is the operator's handle: the console's revoke action names the row
    by id, but the audit record names it by ``token:<label>``, and two rows under
    one label would make both ambiguous. Refusing is the honest answer — a
    suffixed label would silently mint a credential nobody asked for.
    """
    console = _console(tmp_path)
    console.mint_token("laptop", actor=CONSOLE)

    with pytest.raises(ValueError, match="already exists"):
        console.mint_token("laptop", actor=CONSOLE)

    assert [t.label for t in console.machine_tokens()] == ["laptop"]


def test_the_schema_states_one_row_per_label_and_one_per_digest(tmp_path) -> None:
    """The table's own constraints, not this code's: a raw writer is refused too."""
    console = _console(tmp_path)
    _minted, row = console.mint_token("laptop", actor=CONSOLE)

    with closing(sqlite3.connect(str(console.registry.db_path))) as conn, conn:
        digest = conn.execute("SELECT token_digest FROM machine_token").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO machine_token (label, token_digest, created_at)"
                " VALUES ('laptop', 'another-digest', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO machine_token (label, token_digest, created_at)"
                " VALUES ('another-label', ?, 'now')",
                (digest,),
            )
    assert row.id == console.machine_tokens()[0].id


# --- revoking ---------------------------------------------------------------- #


def test_a_revoke_deletes_the_row_and_is_effective_immediately(tmp_path) -> None:
    """The revoke *is* the delete, so there is no cached state and no restart."""
    console = _console(tmp_path)
    minted, _row = console.mint_token("ci", actor=CONSOLE)
    assert console.authenticate_token(minted) is not None

    assert console.revoke_token("ci", actor=CONSOLE) is True
    assert console.machine_tokens() == []
    assert console.authenticate_token(minted) is None, "the next request is refused"

    assert console.revoke_token("ci", actor=CONSOLE) is False, "nothing left to revoke"


def test_a_revoke_is_attributed_and_a_use_is_bookkeeping(tmp_path) -> None:
    """``token.mint`` and ``token.revoke`` answer "who holds a key"; a use does not."""
    console = _console(tmp_path)
    minted, _row = console.mint_token("ci", actor=CONSOLE)
    console.authenticate_token(minted)
    console.revoke_token("ci", actor=CONSOLE)

    rows = [
        (event.actor, event.action, event.target, event.outcome)
        for event in console.registry.list_audit_events()
    ]

    assert rows == [
        (CONSOLE, "credential.set", "credential:console", "ok"),
        (CONSOLE, "token.mint", "token:ci", "ok"),
        (CONSOLE, "token.revoke", "token:ci", "ok"),
    ]


def test_a_revoke_that_matched_no_row_records_nothing(tmp_path) -> None:
    """The audit record's own rule, on this surface: nothing happened, append nothing."""
    console = _console(tmp_path)

    assert console.revoke_token("never-minted", actor=CONSOLE) is False

    actions = [event.action for event in console.registry.list_audit_events()]
    assert actions == ["credential.set"], "a revoke that matched no row wrote a row"


def test_a_revoked_token_does_not_end_the_sessions_or_the_credential(tmp_path) -> None:
    """Three credentials, three scopes: a token's revoke touches only its own row."""
    console = _console(tmp_path)
    session = console.sign_in("service-seam-password-91ab")
    console.mint_token("ci", actor=CONSOLE)
    assert session is not None

    assert console.revoke_token("ci", actor=CONSOLE) is True

    assert console.session(session, touch=False) is SessionState.ACTIVE
    assert console.configured() is True


def test_a_token_does_not_expire_the_way_a_session_does(tmp_path) -> None:
    """A token has no clocks: it is live until it is revoked (ADR-0033).

    Asserted on the seam's own moving clock, so a window can pass under the token
    without the row changing at all.
    """
    now = [dt.datetime.now(dt.UTC)]

    def clock() -> dt.datetime:
        return now[0]

    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    console = ConsoleAuth(registry, clock=clock)
    console.set_password("service-seam-password-91ab", actor=CONSOLE)
    minted, _row = console.mint_token("ci", actor=CONSOLE)

    now[0] += dt.timedelta(days=365)

    assert console.authenticate_token(minted) is not None


def test_a_minted_token_is_random_and_long_enough(tmp_path) -> None:
    """High-entropy randomness, not a password: drawn at the session token's width."""
    first = new_machine_token()

    assert len(first) >= 40, "a short token is a guessable one"
    assert first != new_machine_token()
