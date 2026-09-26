"""The console credential, its human sessions, and the machine tokens (ADR-0033).

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
anonymous is the console's one page — the setup route, whose two form posts are
its own — the liveness route, and the compiled assets under ``/static``
(ADR-0033).

The module also owns the **machine tokens** a script carries: the operator mints
one in the console, sees its plaintext once, and the registry holds only its
digest — so a token is a *second* way to satisfy the gate on the machine surface
and no way at all past the console's pages. A token is not a session and has no
clocks: it lives until it is revoked, and the revoke is a delete, which is what
makes it take effect on the next request with no restart. What a token can never
do is destroy something no durable copy can reconstruct: the machine surface
carries no such verb (ADR-0033), and this module adds none.

Where the credential is written. :class:`ConsoleAuth` is the only writer, and it
writes through :class:`~clear_record.service.store.Registry`, so "the credential
change is a registry row" holds by construction. The **attribution questions** are
audited — ``credential.set`` when the password is set or replaced, ``token.mint``
and ``token.revoke`` for the machine tokens, each with the actor the transport
supplies — and everything else is **bookkeeping**: the session rows (sign-in, the
idle clock, sign-out and the prune) and a token's own last-used instant append no
audit row, on the same line the setup marker is drawn. They are not history of the
project data, and one row per request would bury the record that is.
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
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click
from sqlalchemy.exc import SQLAlchemyError

from clear_record.core import node
from clear_record.core.i18n import tr
from clear_record.service.models import MachineToken

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
#: secret; the digest of it is all the registry ever holds. A **machine token**
#: is the same kind of secret — high-entropy randomness, never a password — so it
#: is drawn at the same width.
_TOKEN_BYTES = 32

#: How long a machine token's label may be. A label is the operator's handle on a
#: script's credential: it is listed, it rides the audit record as
#: ``token:<label>``, and it has to stay readable in a table row. Sixty-four
#: characters is a sentence, which is more than a handle needs.
TOKEN_LABEL_MAX_LENGTH = 64


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
    safe direction. ``OverflowError`` is in that set with the rest: a stored
    ``n`` larger than the derivation can hold is a value this build cannot read,
    and the one thing a damaged row must never do is take the sign-in page down
    with it.
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
    except (ValueError, TypeError, MemoryError, OverflowError):
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


def new_machine_token() -> str:
    """A fresh machine token's plaintext — shown once, stored only as its digest.

    The same shape of secret as a session token (:func:`new_session_token`), and
    deliberately the same call: what differs is not the entropy but the *life* —
    a session has clocks, a token has none and lives until it is revoked.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def require_token_label(label: str) -> str:
    """``label`` as it may be stored, or the refusal that names the rule.

    The label is the one thing about a token a person reads back — the console
    lists it, the audit record's target carries it, and a script's own notes say
    which credential it holds — so it must be an actual handle: non-blank, every
    character **printable** (a newline or a tab would break the row it is listed
    in and the line it is logged on, and a no-break space reads as a blank while
    being a different character), and short enough to read
    (:data:`TOKEN_LABEL_MAX_LENGTH`).
    """
    cleaned = label.strip()
    if not cleaned:
        raise ValueError(tr("a machine token needs a label."))
    if len(cleaned) > TOKEN_LABEL_MAX_LENGTH:
        raise ValueError(
            tr(
                "a token label may be at most {count} characters.",
                count=TOKEN_LABEL_MAX_LENGTH,
            )
        )
    if any(not char.isprintable() for char in cleaned):
        raise ValueError(tr("a token label may contain only printable characters."))
    return cleaned


#: How long a token's recorded use may sit before a request moves it again.
#:
#: A token's **use** is recorded at most once per window, the shape the session
#: idle clock already uses (which writes only when less than half its window
#: remains, so its own writes are at least half a window apart). The reason is
#: sharper here: the gate's write is synchronous inside an ``async`` middleware,
#: so a commit per request would block the event loop for every call a script
#: makes — and a registry another process holds for a moment would then stall the
#: whole node, not just that request (measured: a token request waiting out the
#: driver's busy timeout held up an unrelated ``/health`` probe for seconds). What
#: the console's list has to answer is "is this script still calling?", and a
#: window answers that as well as an instant.
TOKEN_TOUCH_INTERVAL = _dt.timedelta(minutes=5)


def token_digest(token: str) -> str:
    """The registry's form of a token — a plain digest, never the token.

    Shared by both kinds of high-entropy secret the auth surface issues: the
    session cookie's token and the machine tokens a script presents. Neither is a
    password, so neither needs a salt or stretching: a SHA-256 digest cannot be
    read back into the token, and a registry leak hands out no usable credential.
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
    """The credential, the human sessions and the machine tokens, over one registry.

    The console's request gate holds one of these, and so do the routes that set
    the credential, start a session and mint or revoke a token. The policy and the
    clock are attributes rather than globals so a test can shorten a timeout or
    move the clock, and so a process serves one policy at a time. The clock is
    also what stamps a minted token and its uses, so a test that moves it moves
    every auth instant together.
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

        The row it opens, and the expired rows that opening sweeps, are
        :meth:`_open_session`'s — the same path the node's own local session is
        minted through, so neither is a sign-in's alone.
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
        """Write one session row and answer with the token that names it.

        The expired rows a registry accumulates are swept here too, on the way
        in — a new row is about to be written, so this is where the table is kept
        from growing without bound — and best-effort for the reason
        :meth:`session` states: a lock costs the sweep, never the sign-in.
        """
        now = self.clock()
        token = new_session_token()
        self.registry.create_session(
            token_digest(token),
            created_at=now,
            seen_at=now,
            idle_deadline=now + self.policy.idle_timeout,
            absolute_deadline=now + self.policy.absolute_lifetime,
        )
        self._prune(now)
        return token

    def _prune(self, now: _dt.datetime) -> None:
        """Delete the expired rows, tolerating a registry another writer holds.

        Housekeeping, not an act (:meth:`Registry.prune_expired_sessions`): the
        rows this would delete are already over — the same test the caller
        applies — so a **write** that meets the lock another surface holds costs
        the prune and never the request. The rule is the idle touch's, for the
        same reason: raising here would turn a valid request into a 500, and the
        next request writes the same rows away.

        What it cannot do is keep the table from growing while another surface
        holds the registry for a whole busy timeout; that is a dead row waiting
        for the next prune, not a refused operator.
        """
        try:
            self.registry.prune_expired_sessions(now)
        except SQLAlchemyError:
            pass

    def session(self, token: str | None, *, touch: bool = True) -> SessionState:
        """What ``token`` means right now, moving the idle clock when it is live.

        ``touch=False`` is for a request that is **not** being gated — the sign-in
        page asking whether the visitor already has a session — so reading that
        page cannot keep a session alive, and an anonymous request writes nothing.

        A stale session is pruned as it is read: the row is over, and leaving it
        for the next sign-in would keep a dead row in every look-up's way. The
        prune is best-effort and the verdict is not (:meth:`_prune`): a read is
        never refused because the table could not be swept.

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
            self._prune(now)
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

    # --- machine tokens (ADR-0033) ----------------------------------------- #
    #
    # A script's credential, and a **second** way to satisfy the machine
    # surface's half of the gate — never the console's. The plaintext is handed
    # out exactly once, by :meth:`mint_token`; everything after that reads the
    # registry by digest.

    def mint_token(self, label: str, *, actor: str) -> tuple[str, MachineToken]:
        """Mint a labelled token; return ``(the plaintext, the row)``.

        **The plaintext is returned to this caller and never stored**: the
        registry gets the digest, so the console's route is the only place the
        value exists, it is shown once, and no later request — a reload, a second
        tab, this method called again — can reproduce it. The actor is the
        transport's own word, and minting appends ``token.mint`` to the audit
        record, because "who holds a key" is an attribution question.

        A label already in use is a ``ValueError`` naming it (the store's own
        refusal): labels are handles, and silently minting a second token under
        one of them would make the console's revoke action ambiguous.
        """
        minted = new_machine_token()
        row = self.registry.create_machine_token(
            require_token_label(label),
            token_digest(minted),
            created_at=self.clock(),
            actor=actor,
        )
        return minted, row

    def machine_tokens(self) -> list[MachineToken]:
        """Every minted token, oldest first — what the console's list renders."""
        return self.registry.machine_tokens()

    def revoke_token(self, label: str, *, actor: str) -> bool:
        """Revoke the token ``label`` names; True when a row was there.

        The delete *is* the revocation, so the next request bearing the token is
        refused with no restart and nothing cached to expire. Revoking appends
        ``token.revoke`` — the other half of the "who holds a key" question — and
        a label naming nothing revokes nothing and appends nothing.
        """
        return self.registry.revoke_machine_token(label, actor=actor)

    def authenticate_token(
        self, token: str | None, *, touch: bool = True
    ) -> MachineToken | None:
        """The token ``token`` names, or ``None`` — and the last-used write.

        ``None`` for an absent or unknown token: a revoked one, a value from
        another install, or a client's guess. There is no "stale" verdict here,
        unlike a session's — a token has no clocks and no expiry, so a token that
        names a row is live until it does not.

        A **use** moves ``last_used_at``, which is the one write a token's use
        makes — and it is **lazy**: the write happens at most once per
        :data:`TOKEN_TOUCH_INTERVAL`, not on every request, for the reason that
        constant states. The decision is made from the row this call already read,
        so a request that is not due to write performs no write at all: that is
        what keeps one script's burst of calls from serialising the whole node
        behind the registry's write lock. It is also best-effort for the reason
        the gate's other writes are: the token was read and is valid, so a
        registry another writer holds costs the timestamp and never the request —
        raising here would turn a correctly authenticated request into a 500.
        ``touch=False`` is for a caller reading a token without spending a use
        (the console's list never calls this).
        """
        if not token:
            return None
        digest = token_digest(token)
        row = self.registry.machine_token(digest)
        if row is None:
            return None
        now = self.clock()
        if touch and self._use_is_worth_recording(row, now):
            try:
                self.registry.touch_machine_token(digest, used_at=now)
            except SQLAlchemyError:
                pass
        return row

    @staticmethod
    def _use_is_worth_recording(row: MachineToken, now: _dt.datetime) -> bool:
        """Whether this use should move ``last_used_at`` — the lazy clock's test.

        A token that has never been presented is always recorded (its first use
        is the fact the list cannot otherwise show). After that, a use is written
        only once the stored stamp is at least :data:`TOKEN_TOUCH_INTERVAL` old.

        The comparison is a **string** comparison, and exactly the chronological
        one: the registry writes these instants in one fixed-width UTC spelling
        (:func:`~clear_record.service.store._instant`), so two of them order as
        strings in the same order they happened. A stamp this build cannot read —
        empty, or hand-edited into something shorter — sorts before the cutoff and
        is therefore rewritten rather than trusted: the row cannot be inside a
        window nobody can measure.
        """
        if row.last_used_at is None:
            return True
        cutoff = (
            (now - TOKEN_TOUCH_INTERVAL)
            .astimezone(_dt.UTC)
            .isoformat(timespec="microseconds")
        )
        return row.last_used_at <= cutoff


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

    **A publish that cannot write leaves no row behind.** The file is the whole
    point of the local session — a token nothing can read is a credential no
    client will ever present — so a state directory that refuses the write ends
    the session it just opened and lets the failure through (the caller's posture
    is the caller's: :class:`~clear_record.web.app.LocalSessionKeeper` warns and
    retries, a startup gives up on the local session and serves on). Without
    this, a keeper that keeps failing would mint one live row per tick, each held
    for the idle window, for a file that never appears.
    """
    token = console.open_local_session()
    try:
        node.publish_local_session(token)
    except OSError:
        try:
            console.sign_out(token)
        except SQLAlchemyError:
            # The registry is locked as well: the row lapses on its own clocks,
            # and the prune is what sweeps it up later.
            pass
        raise
    return token


def refresh_local_session(console: ConsoleAuth) -> str:
    """Republish the local session **only when the published one is not live**.

    The steady state is one file read and one row look-up: a node whose own client
    is in use keeps its session alive through that use, and a session that is
    still live is left exactly as it is — the file is rewritten only when the
    token in it names no live session, so the file's bytes are a fact about the
    registry rather than a value that changes under a reader. The mint it does
    need can fail to write (:func:`publish_local_session`), and then it raises
    with the row it opened already ended.
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


def _unusable_registry(
    exc: BaseException, path: str | Path, *, writing: bool = False
) -> NoReturn:
    """The refusal for a registry this command cannot deal with, in two frames.

    The **read** frame belongs to the open, and only to the open: the file could
    not be read at all, so nothing was written. The **write** frame belongs to the
    *set* and to the revoke a replacement follows up with: the registry opened,
    and the write the rescue needs met its own refusal (the lock another surface
    holds, a read-only file). A frame that claimed the other's failure would
    misdirect the operator — "cannot read" over a failed *write* sends them to
    look at the file's readability, which is not what went wrong.

    Both name the path the command resolved (the registry file itself, not the
    directory it lives in) and the driver's own reason, and neither is a
    traceback, which is what a failure in the middle of a rescue must not be.
    Neither says which half of the command landed, because the command does not
    know and does not have to: a set that committed before a refused revoke is
    real (the new password proves it when it signs in, and a retry revokes), and a
    refused set changed nothing at all — its unit of work is rolled back with the
    failure.
    """
    if writing:
        _fail(
            tr(
                "cannot write to the registry at {path}: {error}",
                path=path,
                error=exc,
            )
        )
    _fail(
        tr(
            "cannot read the registry at {path}: {error}",
            path=path,
            error=exc,
        )
    )


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
    from clear_record.core import paths
    from clear_record.service.auth import ConsoleAuth
    from clear_record.service.lifecycle import CLI as _CLI
    from clear_record.service.store import Registry

    try:
        registry = Registry.open(data_dir=data_dir)
    except (RuntimeError, OSError, SQLAlchemyError) as exc:
        # A registry this build cannot read — refused by its schema history
        # (`RuntimeError`), unreadable on disk (`OSError`) or not a database at
        # all (`SQLAlchemyError`) — is the whole answer: nothing was written, and
        # the operator gets the place and the reason, not a traceback. The place
        # is resolved rather than described, and it is the same expression the
        # open used to find the file.
        _unusable_registry(exc, paths.registry_path(data_dir))

    console = ConsoleAuth(registry)
    replacing = console.configured()
    password = click.prompt(
        tr("New console password"), hide_input=True, confirmation_prompt=True
    )
    try:
        console.set_password(password, actor=_CLI)
    except ValueError as exc:
        # The rule, in the operator's own words — the length, named once
        # (`require_password`), is the only refusal a *password* can get.
        _fail(str(exc))
    except SQLAlchemyError as exc:
        # The write met the registry's own refusal (the lock another surface
        # holds, a read-only file): nothing was written — the unit of work is
        # rolled back — and it is stated in the **write** frame rather than
        # escaping as a traceback (`_unusable_registry`).
        _unusable_registry(exc, registry.db_path, writing=True)
    if replacing:
        try:
            console.revoke_all()
        except SQLAlchemyError as exc:
            # A replacement that could not end the sessions it replaced is not
            # the rescue it promises: the credential *is* set (the sentence says
            # the place and the error, not which half landed), and a retry is
            # what ends them.
            _unusable_registry(exc, registry.db_path, writing=True)
    click.echo(
        tr(
            "the console password is {action} in {path}.",
            action=tr("replaced") if replacing else tr("set"),
            path=registry.db_path,
        )
    )
    if replacing:
        click.echo(
            tr("every signed-in browser is now signed out; sign in again at /web/setup")
        )
    else:
        click.echo(tr("next: start the console and sign in at /web/setup"))
    return 0


def register(group: click.Group) -> None:
    """Add the rescue command to the CLI group (entry-point discovery)."""
    group.add_command(_password)


__all__ = [
    "DEFAULT_SESSION_POLICY",
    "LOCAL_SESSION_REFRESH_S",
    "PASSWORD_MIN_LENGTH",
    "TOKEN_LABEL_MAX_LENGTH",
    "TOKEN_TOUCH_INTERVAL",
    "ConsoleAuth",
    "SessionPolicy",
    "SessionState",
    "hash_password",
    "new_machine_token",
    "new_session_token",
    "publish_local_session",
    "refresh_local_session",
    "register",
    "require_password",
    "require_token_label",
    "token_digest",
    "verify_password",
]
