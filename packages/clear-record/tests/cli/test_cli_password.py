"""The rescue command: set or replace the console credential with no browser.

``clear-record password`` is the way back into a console whose credential is lost
or broken (ADR-0033). It is the one credential writer that is not the console: no
node has to be running, no session exists, and no browser is involved — it opens
the registry itself. What these tests hold to: the credential it writes is the one
the console's sign-in accepts, a *replacement* ends every session the old
credential had opened, the password never reaches the output, and a registry this
build cannot read is refused rather than half-written.
"""

from __future__ import annotations

from click.testing import CliRunner
from sqlalchemy.exc import OperationalError

from clear_record.cli import cli
from clear_record.service.auth import ConsoleAuth, SessionState, verify_password
from clear_record.service.lifecycle import CLI
from clear_record.service.store import Registry

#: The credential each test writes, and the one it replaces.
NEW_PASSWORD = "the-replacement-password"
OLD_PASSWORD = "the-original-password"


def _output(result) -> str:
    """Everything the command said, on either stream (Click keeps them apart)."""
    return result.output + (getattr(result, "stderr", "") or "")


def _rescue(data_dir, *, typed: str | None = None):
    """Invoke the rescue command against ``data_dir``, typed twice at the prompt."""
    entered = typed if typed is not None else NEW_PASSWORD
    return CliRunner().invoke(
        cli._build_group(),
        ["password", "--data-dir", str(data_dir)],
        input=f"{entered}\n{entered}\n",
    )


def _registry(data_dir) -> Registry:
    return Registry.open(data_dir=data_dir)


def test_the_rescue_sets_the_credential_and_prints_the_next_step(tmp_path) -> None:
    result = _rescue(tmp_path)

    assert result.exit_code == 0, _output(result)
    assert verify_password(NEW_PASSWORD, _registry(tmp_path).credential())
    assert "next" in _output(result).lower()
    # The prompt is hidden and the answer is never echoed: the terminal output is
    # not a place the password may appear.
    assert NEW_PASSWORD not in _output(result)


def test_the_rescue_replaces_the_credential_and_ends_every_session(tmp_path) -> None:
    """A replacement that left an old device signed in would not be a rescue."""
    registry = _registry(tmp_path)
    console = ConsoleAuth(registry)
    console.set_password(OLD_PASSWORD, actor=CLI)
    token = console.sign_in(OLD_PASSWORD)
    assert token is not None

    result = _rescue(tmp_path)

    assert result.exit_code == 0, _output(result)
    stored = registry.credential()
    assert verify_password(NEW_PASSWORD, stored)
    assert not verify_password(OLD_PASSWORD, stored)
    assert ConsoleAuth(registry).session(token) is SessionState.UNKNOWN
    assert "signed out" in _output(result)


def test_the_rescue_refuses_a_password_under_the_rule(tmp_path) -> None:
    result = _rescue(tmp_path, typed="short")

    assert result.exit_code != 0
    assert "at least 8 characters" in _output(result)
    assert _registry(tmp_path).credential() is None


def test_the_rescue_fails_closed_on_an_unreadable_registry(tmp_path) -> None:
    """A registry that is not a database is refused, and nothing is written.

    The whole point of the command is a console that will not let you in, so a
    registry it cannot read has to be a sentence rather than a traceback — and the
    file it refused stays exactly as it was.
    """
    broken = tmp_path / "registry.sqlite3"
    broken.write_bytes(b"this is not a database")

    result = _rescue(tmp_path)

    assert result.exit_code == 1
    assert "cannot read the registry" in _output(result)
    assert broken.read_bytes() == b"this is not a database"


def test_the_rescue_states_a_refused_write_instead_of_raising(
    tmp_path, monkeypatch
) -> None:
    """A write the registry refuses is the *write* sentence, not the open's.

    The window is real — a running node's write lock, or another surface
    mid-migration, both held against the driver's busy timeout — and it is the one
    thing the rescue must not answer with: a traceback, at the moment the operator
    has no other way in. The frame matters too: this registry was *read* (the
    credential was looked up to decide the command's half), so claiming it cannot
    be read would send the operator after a fault that is not there. The set's
    unit of work is rolled back with the failure, so nothing is half-written
    either.
    """
    _registry(tmp_path)  # the registry the command opens

    def locked(self, password, *, actor):
        raise OperationalError(
            "UPDATE console_credential", {}, Exception("database is locked")
        )

    monkeypatch.setattr(ConsoleAuth, "set_password", locked)

    result = _rescue(tmp_path)

    assert result.exit_code == 1
    assert "cannot write to the registry" in _output(result)
    assert str(tmp_path / "registry.sqlite3") in _output(result), "the file it named"
    assert isinstance(result.exception, SystemExit), "not a traceback"
    assert _registry(tmp_path).credential() is None


def test_the_rescue_states_a_refused_revoke_instead_of_raising(
    tmp_path, monkeypatch
) -> None:
    """The write a *replacement* follows up with gets the write sentence too.

    ``revoke_all`` is the half that makes a replacement a rescue — a credential
    change that left an old device signed in would not be one — and it fails
    against the same lock. The frame is the write's, like the set's above, because
    the same thing went wrong: a registry that opened and answered, whose write
    was refused. The credential *is* set by then (the sentence names the place and
    the error, not which half landed), and a retry is what ends the sessions: it
    reports the credential "replaced" and revokes.
    """
    console = ConsoleAuth(_registry(tmp_path))
    console.set_password(OLD_PASSWORD, actor=CLI)
    token = console.sign_in(OLD_PASSWORD)
    assert token is not None

    def locked(self) -> int:
        raise OperationalError(
            "DELETE FROM console_session", {}, Exception("database is locked")
        )

    monkeypatch.setattr(ConsoleAuth, "revoke_all", locked)

    result = _rescue(tmp_path)

    assert result.exit_code == 1
    assert "cannot write to the registry" in _output(result)
    assert isinstance(result.exception, SystemExit), "not a traceback"
    assert verify_password(NEW_PASSWORD, _registry(tmp_path).credential())
    assert ConsoleAuth(_registry(tmp_path)).session(token) is SessionState.ACTIVE
