"""The console credential and the human sessions it authorizes (ADR-0033).

The auth position is one human, one credential and many actors. The credential is
**one password**, set on the console's first run or replaced by the rescue command
:func:`register` adds to the CLI; it is stored in the registry as a **salted
hash** and never anywhere else — not in ``config.toml``, not in the agent setup
file, not in a log line and not in the diagnostics bundle. Signing in holds a
**server-side session**: the browser gets an opaque token in an ``HttpOnly``
cookie, the registry holds only the token's digest, and every request re-reads the
row — so signing out and revoking every session take effect on the **next**
request, with no restart and nothing cached in the process.

Two clocks, both the registry's:

- an **idle timeout** — a session whose last request is older than
  :data:`DEFAULT_SESSION_POLICY`'s ``idle_timeout`` is over; every accepted
  request moves it forward;
- an **absolute lifetime** — a session never outlives ``absolute_lifetime`` from
  the moment it was created, however busy it is.

What is *not* here. No accounts, no usernames and no per-surface authority
matrix: the trust boundary is the operating-system account — the one subject a
surface's `actor` word is really about — and an operation that destroys something
no durable copy can reconstruct is gated at the act, not by a token. What stays
anonymous is one page and one route (ADR-0033); machine tokens are a sibling
ticket's half, and this module is what they will sit beside.

Where the credential is written. :class:`ConsoleAuth` is the only writer, and it
writes through :class:`~clear_record.service.store.Registry`, so "the credential
change is a registry row" holds by construction. The *set* is audited
(``credential.set``, with the actor the transport supplies); the session rows are
**bookkeeping** — sign-in, the idle clock, sign-out and the prune — and append no
audit row, on the same line the setup marker is drawn: they are not history of the
project data, and one row per request would bury the record that is. The one
mutation of security state that *is* an attribution question — who holds the key
— is answered by the ``credential.set`` row.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import enum
import hashlib
import hmac
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, NoReturn

import click
from sqlalchemy.exc import SQLAlchemyError

from clear_record.core import node
from clear_record.core.i18n import tr

if TYPE_CHECKING:  # the store imports this module's constants, never the reverse
    from clear_record.service.store import Registry

#: The shortest password the console accepts, set or replaced. Length is the one
#: rule that buys the most: nothing here counts classes of characters, and a
#: passphrase that is long and memorable beats a short one with punctuation.
PASSWORD_MIN_LENGTH = 8

#: The key-derivation encoding's scheme word, so a future scheme is a second word
#: in the stored string rather than a migration of what a password *is*.
_SCRYPT = "scrypt"

#: The scrypt parameters the credential is stored with: 2**14 iterations, block
#: size 8, one parallelisation, 32 derived bytes and a fresh 16-byte salt per
#: install. Recorded inside each stored value, so a verify reads the parameters it
#: was written with rather than this build's.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_DERIVED_BYTES = 32
_SALT_BYTES = 16

#: The session token's entropy. 32 random bytes, URL-safe, are the cookie's whole
#: secret; the digest of it is all the registry ever holds.
_TOKEN_BYTES = 32


def _utcnow() -> _dt.datetime:
    """The clock the sessions are judged by — one place, so a test can move it."""
    return _dt.datetime.now(_dt.UTC)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """``password`` as the stored value: ``scrypt$n$r$p$salt$hash``.

    The salt is fresh per call — per install, and per replacement — so two
    installs that happen to choose the same password store different values and a
    stolen registry cannot be attacked with one precomputed table. The parameters
    ride in the value so a later build can raise them without invalidating what is
    already stored.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DERIVED_BYTES,
    )
    return "$".join(
        (
            _SCRYPT,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64(salt),
            _b64(derived),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    """Whether ``password`` is the one ``encoded`` was derived from.

    The comparison is :func:`hmac.compare_digest`, so a wrong password costs the
    same time whatever of it matched. An unparsable value — a version of the
    encoding this build does not know, or a corrupt row — verifies **nothing**:
    a credential nobody can reproduce is not a credential, and refusing is the
    safe direction.
    """
    if not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != _SCRYPT:
        return False
    try:
        n, r, p = (int(parts[1]), int(parts[2]), int(parts[3]))
        salt, expected = _unb64(parts[4]), _unb64(parts[5])
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


def require_password(password: str) -> str:
    """``password`` as it may be stored, or the refusal that names the rule."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(
            tr(
                "the password must be at least {count} characters.",
                count=PASSWORD_MIN_LENGTH,
            )
        )
    return password


def new_session_token() -> str:
    """A fresh session token: the cookie's value and the registry's digest input."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_digest(token: str) -> str:
    """The registry's form of a session token — a plain digest, never the token.

    A session token is high-entropy randomness, not a password, so it needs no
    salt or stretching: a SHA-256 digest cannot be read back into the token, and a
    registry leak hands out no usable cookie.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class SessionPolicy:
    """How long a session lives: idle timeout and absolute lifetime.

    Both are **duration from** instants the registry records, so the policy can
    change between two requests without rewriting the sessions that are already
    open — the shorter of the two wins, and the next accepted request moves the
    idle deadline under whatever policy is in force then.
    """

    #: How long a session may sit **unused**. Every accepted request moves it.
    idle_timeout: _dt.timedelta = _dt.timedelta(hours=12)
    #: How long a session lives **at most**, from the moment it was created.
    absolute_lifetime: _dt.timedelta = _dt.timedelta(days=30)


#: The policy a console starts with. One human, one browser, a working day with
#: room to be interrupted: the idle timeout is the day and the absolute lifetime
#: is the month. Stated in ``docs/service-deployment.md`` where the operator
#: meets it.
DEFAULT_SESSION_POLICY = SessionPolicy()


class SessionState(enum.StrEnum):
    """What one presented cookie means, as the request gate reads it."""

    #: The token names a live session that is inside both windows.
    ACTIVE = "active"
    #: The token was issued by this registry and the session is over — past the
    #: idle timeout or past its absolute lifetime. A sign-in is the answer.
    STALE = "stale"
    #: No token was presented, or the token names no row: a cookie from another
    #: install, a revoked session, or a browse after sign-out.
    UNKNOWN = "unknown"


class ConsoleAuth:
    """The console credential and its sessions, over one registry.

    The console's request gate holds one of these, and the routes that set the
    credential or start a session hold the same one. The policy and the clock are
    attributes rather than globals so a test can shorten a timeout or move the
    clock, and so a process serves one policy at a time.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        policy: SessionPolicy = DEFAULT_SESSION_POLICY,
        clock: Callable[[], _dt.datetime] = _utcnow,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.clock = clock

    # --- the credential --------------------------------------------------- #
    def configured(self) -> bool:
        """Whether a credential has been set — the first run's whole question."""
        return self.registry.credential() is not None

    def set_password(self, password: str, *, actor: str) -> None:
        """Set or replace the credential; ``actor`` is the transport's own word.

        The caller has already validated the length where a *person* is waiting
        for a message (the console form and the rescue command both do), and the
        rule is stated once more here (:func:`require_password`) so no other
        caller can store a value the rule refuses.
        """
        self.registry.store_credential(
            hash_password(require_password(password)), actor=actor
        )

    # --- sessions --------------------------------------------------------- #
    def sign_in(self, password: str) -> str | None:
        """Start a session when ``password`` matches; return its token, or ``None``.

        ``None`` is the whole answer for a wrong password *and* for a credential
        that was never set: the page that asks knows which case it is in, and a
        caller is told nothing a guess could use.

        No ``actor`` and no audit row: a sign-in is not a mutating *service* entry
        point — it moves no project data — so the record's vocabulary needs no
        word for it (see the module doc). The set that *is* attributed is the
        credential's own.

        The expired rows a registry accumulates are pruned here, on the way in —
        signing in is the one moment a new row is about to be written, so it is
        where the table is kept from growing without bound.
        """
        encoded = self.registry.credential()
        if encoded is None or not verify_password(password, encoded):
            return None
        return self._open_session()

    def open_local_session(self) -> str:
        """Open a session for **this node's own machine**; return its token.

        The command line is a client of the node (ADR-0032) and runs as the same
        operating-system user, which ADR-0033 puts *inside* the trust boundary
        this gate defends: the node hands that user one session rather than a
        password prompt, and a client on the machine presents it as the session
        cookie (:data:`clear_record.core.node.LOCAL_SESSION_FILENAME`).

        It is a session like any other — the same policy's idle and absolute
        clocks, the same rows, ended by ``revoke_all`` and by :meth:`sign_out` —
        and the request gate has no branch for it. What is special is only *how it
        is handed out*: through a file the node's own uid can read, which is a
        capability that uid already had (it can write the registry).
        """
        return self._open_session()

    def _open_session(self) -> str:
        """Write one session row and answer with the token that names it."""
        now = self.clock()
        token = new_session_token()
        self.registry.create_session(
            token_digest(token),
            created_at=now,
            seen_at=now,
            idle_deadline=now + self.policy.idle_timeout,
            absolute_deadline=now + self.policy.absolute_lifetime,
        )
        self.registry.prune_expired_sessions(now)
        return token

    def session(self, token: str | None, *, touch: bool = True) -> SessionState:
        """What ``token`` means right now, moving the idle clock when it is live.

        ``touch=False`` is for a request that is **not** being gated — the sign-in
        page asking whether the visitor already has a session — so reading that
        page cannot keep a session alive, and an anonymous request writes nothing.

        A stale session is pruned as it is read: the row is over, and leaving it
        for the next sign-in would keep a dead row in every look-up's way.

        **The idle clock is moved lazily, and a busy registry cannot refuse a live
        session.** Two things follow from the gate running this on every request:

        - the write happens only when less than half the idle window is left, so a
          page's burst of requests (a document and its assets, htmx polls) is one
          write rather than one per request — the deadline it leaves is the same
          one the first of them would have written;
        - a ``touch`` that meets a registry another writer holds for a moment is
          **not** an error: the session was read and is live, so the request is
          authenticated and merely keeps the clock it had. Raising here would turn
          a valid request into a 500, which is exactly what this gate must never
          answer an idle-timeout question with.
        """
        if not token:
            return SessionState.UNKNOWN
        digest = token_digest(token)
        row = self.registry.get_session(digest)
        if row is None:
            return SessionState.UNKNOWN
        now = self.clock()
        if now >= row.idle_deadline or now >= row.absolute_deadline:
            self.registry.prune_expired_sessions(now)
            return SessionState.STALE
        if touch and now + self.policy.idle_timeout / 2 > row.idle_deadline:
            try:
                self.registry.touch_session(
                    digest, seen_at=now, idle_deadline=now + self.policy.idle_timeout
                )
            except SQLAlchemyError:
                # Another writer holds the registry for a moment; the next
                # request moves the clock. The session is live either way.
                pass
        return SessionState.ACTIVE

    def sign_out(self, token: str | None) -> bool:
        """End the session ``token`` names; True when a row was there to end."""
        if not token:
            return False
        return self.registry.end_session(token_digest(token))

    def revoke_all(self) -> int:
        """End **every** session; the count of rows that were there to end.

        The act a borrowed browser stops being a way in by, and the act a replaced
        credential needs to mean anything on a device the operator has lost: it
        takes effect on the next request, from any surface, with no restart.
        """
        return self.registry.end_all_sessions()


#: How often a running node checks that the session it published for its own
#: machine's clients still names a live one, and re-opens it when it does not.
#:
#: The node's own session is subject to the same clocks as a browser's and there
#: is nobody on the client's side to sign in again, so the node is what keeps it
#: current: across an idle window (a command line used less often than the idle
#: timeout), across the absolute lifetime of a node left running, and after a
#: ``revoke_all`` — which does end it, and the next check then mints a fresh one
#: for the node's own machine. A minute is far inside the shortest shipped
#: window, and one look-up a minute is nothing beside the log the node writes.
LOCAL_SESSION_REFRESH_S = 60.0


def publish_local_session(console: ConsoleAuth) -> str:
    """Open a session for this node's own machine and publish its token.

    Returns the token it wrote. What the file is, who may read it and what a
    stale one means are stated with it
    (:data:`clear_record.core.node.LOCAL_SESSION_FILENAME`).
    """
    token = console.open_local_session()
    node.publish_local_session(token)
    return token


def refresh_local_session(console: ConsoleAuth) -> str:
    """Republish the local session **only when the published one is not live**.

    The steady state is one file read and one row look-up: a node whose own client
    is in use keeps its session alive through that use, and a session that is
    still live is left exactly as it is — the file is rewritten only when the
    token in it names no live session, so the file's bytes are a fact about the
    registry rather than a value that changes under a reader.
    """
    token = node.local_session()
    if token is not None and console.session(token, touch=False) is SessionState.ACTIVE:
        return token
    return publish_local_session(console)


# --- the rescue command ----------------------------------------------------- #
#
# The CLI reaches the command through the `clear_record.commands` entry point
# (ADR-0013), exactly as `diagnose` does, so `clear_record.cli` never imports the
# service layer. It is not a facade over a running node and must not be: the case
# it exists for is the one where the console will not let you in.


def _fail(message: str) -> NoReturn:
    """State a refusal on stderr and exit non-zero — the whole answer.

    ``SystemExit`` rather than a returned status: a Click command's return value
    is ignored in standalone mode (which is how a runner invokes it), so a failure
    that only *returned* 1 would read as success to everything but the console
    script. The same shape the CLI's other fail-closed paths use.
    """
    click.echo(message, err=True)
    raise SystemExit(1)


@click.command(
    name="password",
    help=tr("set or replace the console password, without a browser (the rescue)"),
)
@click.option(
    "--data-dir",
    default=None,
    envvar="CR_DATA_DIR",
    show_envvar=True,
    help=tr(
        "override the app data directory "
        "(default: CR_DATA_DIR / the platform data directory)"
    ),
)
def _password(data_dir: str | None) -> int:
    """Set or replace the credential in the registry itself.

    No node has to be running and no browser is involved: this opens the registry
    directly, hashes what the operator types at the prompt, and writes it. Two
    things about a *replacement*: it ends every session (a credential change that
    left an old device signed in would not be a rescue), and it cannot be undone —
    the previous value is a hash and stays a hash.
    """
    from clear_record.service.auth import ConsoleAuth
    from clear_record.service.lifecycle import CLI as _CLI
    from clear_record.service.store import Registry

    try:
        registry = Registry.open(data_dir=data_dir)
    except (RuntimeError, OSError, SQLAlchemyError) as exc:
        # A registry this build cannot read — refused by its schema history
        # (`RuntimeError`), unreadable on disk (`OSError`) or not a database at
        # all (`SQLAlchemyError`) — is the whole answer: nothing was written, and
        # the operator gets the place and the reason, not a traceback.
        _fail(
            tr(
                "cannot read the registry at {path}: {error}",
                path=data_dir or tr("the default data directory"),
                error=exc,
            )
        )

    console = ConsoleAuth(registry)
    replacing = console.configured()
    password = click.prompt(
        tr("New console password"), hide_input=True, confirmation_prompt=True
    )
    try:
        console.set_password(password, actor=_CLI)
    except ValueError as exc:
        _fail(str(exc))
    if replacing:
        console.revoke_all()
    click.echo(
        tr(
            "the console password is {action} in {path}.",
            action=tr("replaced") if replacing else tr("set"),
            path=registry.db_path,
        )
    )
    if replacing:
        click.echo(
            tr("every signed-in browser is now signed out; sign in again at /setup")
        )
    else:
        click.echo(tr("next: start the console and sign in at /setup"))
    return 0


def register(group: click.Group) -> None:
    """Add the rescue command to the CLI group (entry-point discovery)."""
    group.add_command(_password)


__all__ = [
    "DEFAULT_SESSION_POLICY",
    "LOCAL_SESSION_REFRESH_S",
    "PASSWORD_MIN_LENGTH",
    "ConsoleAuth",
    "SessionPolicy",
    "SessionState",
    "hash_password",
    "new_session_token",
    "publish_local_session",
    "refresh_local_session",
    "register",
    "require_password",
    "token_digest",
    "verify_password",
]
