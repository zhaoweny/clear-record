"""The console credential and its sessions, at the service seam (ADR-0033).

What the console's request gate reads: a salted hash and two clocks. These tests
drive the seam directly — no HTTP — so the credential's storage, the session's
verdicts (live, idle, expired, revoked) and the schema's own "one credential" rule
are asserted where they live rather than through a page.

The web layer's half is ``tests/web/test_web_auth.py``: the cookie, the redirect,
the anonymous surface. This file is about what the registry holds.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from contextlib import closing

import pytest

from clear_record.service.auth import (
    DEFAULT_SESSION_POLICY,
    PASSWORD_MIN_LENGTH,
    ConsoleAuth,
    SessionPolicy,
    SessionState,
    hash_password,
    new_session_token,
    require_password,
    token_digest,
    verify_password,
)
from clear_record.service.lifecycle import CLI, CONSOLE
from clear_record.service.store import Registry

#: A password long enough for the rule and distinctive enough to search a file
#: for: "is the plaintext stored?" is the question this value answers.
PASSWORD = "service-seam-password-91ab"


def _console(tmp_path, **policy) -> ConsoleAuth:
    """A seam over a temp registry, with a policy a test can shorten."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    return ConsoleAuth(registry, policy=SessionPolicy(**policy))


def _sessions(registry: Registry) -> int:
    """How many session rows the registry holds — read straight from the file."""
    with closing(sqlite3.connect(str(registry.db_path))) as conn, conn:
        return int(conn.execute("SELECT COUNT(*) FROM console_session").fetchone()[0])


# --- the credential ---------------------------------------------------------- #


def test_the_credential_is_a_hash_with_a_fresh_salt_per_write() -> None:
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first.startswith("scrypt$")
    assert first != second, "the salt is not fresh per write"
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)
    # The parameters ride in the value, so a verify reads what it was written
    # with: 6 fields, scheme first.
    assert len(first.split("$")) == 6


def test_verification_refuses_a_wrong_or_unreadable_value() -> None:
    stored = hash_password(PASSWORD)

    assert not verify_password(PASSWORD + "x", stored)
    assert not verify_password("", stored)
    assert not verify_password(PASSWORD, None)
    for broken in ("", "nonsense", "scrypt$1$2$3$4", "bcrypt$1$2$3$4$5$6"):
        assert not verify_password(PASSWORD, broken), broken


def test_the_password_rule_is_one_statement() -> None:
    assert require_password("x" * PASSWORD_MIN_LENGTH) == "x" * PASSWORD_MIN_LENGTH
    with pytest.raises(ValueError, match="at least"):
        require_password("x" * (PASSWORD_MIN_LENGTH - 1))


def test_the_schema_holds_one_credential_and_the_plaintext_is_never_stored(
    tmp_path,
) -> None:
    console = _console(tmp_path)
    assert console.configured() is False

    console.set_password(PASSWORD, actor=CONSOLE)

    assert console.configured() is True
    registry = console.registry
    assert PASSWORD.encode() not in registry.db_path.read_bytes()

    # The table's own rule, not this code's: a second credential row is refused
    # by the file, so "one install, one credential" cannot be broken by a writer
    # that ignores the service.
    with closing(sqlite3.connect(str(registry.db_path))) as conn, conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO console_credential (id, encoded, updated_at)"
                " VALUES (2, 'x', 'now')"
            )


def test_the_credential_set_is_recorded_and_the_session_writes_are_not(
    tmp_path,
) -> None:
    console = _console(tmp_path)

    console.set_password(PASSWORD, actor=CLI)
    token = console.sign_in(PASSWORD)
    assert token is not None
    console.session(token)
    console.sign_out(token)

    rows = [
        (event.actor, event.action, event.target, event.outcome)
        for event in console.registry.list_audit_events()
    ]

    assert rows == [(CLI, "credential.set", "credential:console", "ok")]


# --- sessions ---------------------------------------------------------------- #


def test_signing_in_needs_a_credential_and_the_right_password(tmp_path) -> None:
    console = _console(tmp_path)

    assert console.sign_in(PASSWORD) is None, "no credential is set"

    console.set_password(PASSWORD, actor=CONSOLE)

    assert console.sign_in("not-the-password") is None
    token = console.sign_in(PASSWORD)
    assert token is not None and len(token) > 20
    assert console.session(token) is SessionState.ACTIVE
    assert _sessions(console.registry) == 1


def test_the_registry_holds_the_token_digest_and_nothing_else(tmp_path) -> None:
    console = _console(tmp_path)
    console.set_password(PASSWORD, actor=CONSOLE)
    token = console.sign_in(PASSWORD)
    assert token is not None
    registry = console.registry

    raw = registry.db_path.read_bytes()

    assert token.encode() not in raw
    assert token_digest(token).encode() in raw
    assert console.session(token) is SessionState.ACTIVE
    assert console.session("a-made-up-token") is SessionState.UNKNOWN
    assert console.session(None) is SessionState.UNKNOWN


def test_an_idle_past_the_timeout_is_stale_and_is_pruned(tmp_path) -> None:
    """A real timer, not a patched clock: the row is what expires."""
    console = _console(tmp_path, idle_timeout=dt.timedelta(milliseconds=250))
    console.set_password(PASSWORD, actor=CONSOLE)
    token = console.sign_in(PASSWORD)
    assert token is not None
    assert console.session(token) is SessionState.ACTIVE

    time.sleep(0.4)

    assert console.session(token) is SessionState.STALE
    assert _sessions(console.registry) == 0, "a stale session is left behind"


def test_the_absolute_lifetime_does_not_move_with_use(tmp_path) -> None:
    """Busy is not immortal: the idle clock moves, the absolute one never does."""
    console = _console(
        tmp_path,
        idle_timeout=dt.timedelta(hours=1),
        absolute_lifetime=dt.timedelta(seconds=30),
    )
    console.set_password(PASSWORD, actor=CONSOLE)
    started = console.clock()
    token = console.sign_in(PASSWORD)
    assert token is not None

    console.clock = lambda: started + dt.timedelta(seconds=20)
    assert console.session(token) is SessionState.ACTIVE  # inside both windows

    console.clock = lambda: started + dt.timedelta(seconds=31)
    assert console.session(token) is SessionState.STALE


def test_signing_out_ends_one_session_and_revoking_ends_every_one(tmp_path) -> None:
    console = _console(tmp_path)
    console.set_password(PASSWORD, actor=CONSOLE)
    first = console.sign_in(PASSWORD)
    second = console.sign_in(PASSWORD)
    assert first and second and _sessions(console.registry) == 2

    assert console.sign_out(first) is True
    assert console.session(first) is SessionState.UNKNOWN
    assert console.session(second) is SessionState.ACTIVE
    assert console.sign_out(first) is False, "the session is already gone"

    assert console.revoke_all() == 1
    assert console.session(second) is SessionState.UNKNOWN
    assert _sessions(console.registry) == 0
    assert console.revoke_all() == 0


def test_a_default_policy_is_a_working_day_and_a_month() -> None:
    """The shipped windows, stated here so a change to them is a decision."""
    assert DEFAULT_SESSION_POLICY.idle_timeout == dt.timedelta(hours=12)
    assert DEFAULT_SESSION_POLICY.absolute_lifetime == dt.timedelta(days=30)


def test_a_new_session_token_is_unguessable_and_its_digest_is_stable() -> None:
    tokens = {new_session_token() for _ in range(32)}

    assert len(tokens) == 32
    one = next(iter(tokens))
    assert token_digest(one) == token_digest(one)
    assert token_digest(one) != one
    assert len(token_digest(one)) == 64
