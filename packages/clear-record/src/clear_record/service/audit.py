"""The audit record: who called the service, what they touched, and how it ended.

ADR-0033 decides it: **a mutating service call appends one row** —
``(at, actor, action, target, outcome)`` — to a record that is **append-only**,
and the **actor is a required argument** on the service's mutating entry points,
so no surface can forget it and no surface can forge it. Two calls append nothing,
because nothing happened: a **conditional** write whose statement matched no row,
and a **key miss** — an id that names no row at all.

What an actor is. One of the words the transport supplies about itself, never a
string a *caller* chose (:data:`~clear_record.service.lifecycle.ACTORS`):
``console`` for the local console, ``api`` for the HTTP JSON API, ``mcp`` for the
stdio MCP adapter, ``cli`` for the command line — and ``queue``, which is how the
node's own queue signs the moves it makes on the node's behalf.
:func:`require_actor` is the single place that decides, so a surface cannot
quietly invent a word, and it refuses ``None``, a non-string and an empty string
with the same voice: a value the record cannot attribute a row to is not an
actor. A run's ``origin`` is a *related* vocabulary — the surface that asked —
but it is not the actor: ``origin`` is a column a caller may set through a run
request, the actor is the transport, and the two are recorded separately so a
client cannot name itself in the audit record by filling in a body field.

What is recorded. Every mutation of the **registry** — the service's own state —
and every mutation of a **draft chain**, which is a meeting's file the service
owns. One exception inside the registry, named here because the rule sounds like
it covers it: the credential and session tables' bookkeeping writes — sign-in,
the idle clock, sign-out, the expired prune — append nothing, because a session
is not history of the project data and one row per request would bury the record
that is. The credential's *set* is recorded, because who holds the key is an
attribution question. What the app writes for itself (the setup marker, the
external MCP client's config) is a preference, not project data, and is not in
this record either. Mutating store methods are audited by
:func:`recorded`; the operations above them — a draft write, a run's start — use
:func:`refused_call` for the refusals *they* own.

:func:`recorded` is how a store method is audited. It wraps the method, so a call
appends a row whether it returned or raised — except where nothing happened: a
**conditional** write whose statement matched no row, and a **key miss**, both
below:

- the call returned — one row, ``outcome='ok'``, appended in a unit of work of
  its own, immediately after the write it names. A **conditional** write is the
  exception, because what it returns *is* its statement's own answer: when the
  statement matched no row (``claim_run``'s lost race, a state the move is not
  legal from, an id that names no row), nothing was written and nothing is
  recorded — ``recorded(…, conditional=True)`` reads that answer instead of
  assuming the call acted;
- the call raised — one row, ``outcome='failed'``, appended the same way —
  **except** a :class:`KeyError`, the key miss the next paragraph excludes, which
  appends nothing. A refusal is the row an audit record exists for, and it
  *cannot* be recorded in the transaction it belonged to: that transaction is
  rolled back with the failure, so the row is written after it, in one of its own.

**Which refusals are rows, and which are not.** A ``failed`` row is appended
where the refusal is the *service's own policy answer*: this draft version is
stale, a run is already in flight for this meeting, this tape is not managed, the
name you gave is taken. A **key miss** is not: an unknown id is a lookup that
found nothing (:class:`KeyError`, answered as a 404 by the HTTP surfaces — the
MCP adapter answers a ``ToolError`` and the command line a sentence), not an
operation the service decided against — there is no subject to name, and the row
would say only that someone asked for something that does not exist. That line is
what :func:`refused_call` states at each entry point, as the exception types it
records.

**The row is not worth the caller's outcome.** The append is a write of its own,
so the registry can refuse it — another surface holds its write lock past the
driver's busy timeout, or the file cannot be written at all — and every caller
here keeps what the *call* answered: a refused call still raises its own refusal
(a database error from the record of it is not the service's answer, and it is
what the HTTP surfaces would have answered a bare 500 for), and a call that
returned still returns. The refusal is therefore tolerated, as
:func:`~clear_record.service.store._write_ahead_log` and the token's touch
tolerate it, and the row it costs is **stated** rather than raised: the node's log
carries one ``audit.row_lost`` record (:data:`ROW_LOST`, component ``audit``) with
the actor, action, target and outcome the row would have carried and the refusal
that stopped it. That name is the whole of the narrowing: "one row per mutating
call" holds *unless the registry will not take the row*, and where that happens a
reader meets the loss in the log. The attempt is bounded — the connection's busy
timeout, once
(:meth:`~clear_record.service.store.Registry.record_refusal` is where the failure
path spends that budget rather than paying it twice).

That the row is not in the same transaction as the write is the price of
recording refusals at all, and it is the honest shape: the record is the
service's account of its calls, not a second copy of the write-ahead log. The
target is read off the **arguments** of the call (a :data:`Target`), never off
its result, so a refused call — which has no result — and a call that returned
name their subject the same way. A call that never named an actor (a
programming error, which the method's own signature raises as a ``TypeError``)
appends nothing: there is no actor to attribute the row to, which is the whole
point of the argument. A call that named a *bad* actor is refused by
:func:`require_actor` before it runs and before any row: the value is the first
thing the gate reads, and a word outside the vocabulary would make the record
unreadable.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from clear_record.core.diagnostics import log_event
from clear_record.service.lifecycle import ACTORS

#: How a call ended, as the record states it. Two values, and no third: a call
#: either did what it was asked or was refused.
OK = "ok"
FAILED = "failed"

#: What the row's actor did, as a term the vocabulary accepts.
OUTCOMES: tuple[str, ...] = (OK, FAILED)

#: The log event that states a row the record owed and did not get. The record's
#: own sentence — "one row per mutating call" — is narrower than it sounds by
#: exactly this: a registry that refuses the append (a lock, a full disk) costs
#: the row, and this is the name under which the loss is written to the node's
#: log (component ``audit``) so it cannot pass unremarked.
ROW_LOST = "audit.row_lost"

#: A target rule: a format string over the call's bound arguments (``"{slug}"``),
#: or a function of them. Read off the arguments rather than the result so that a
#: refused call names its subject exactly as a successful one does.
Target = str | Callable[[Mapping[str, Any]], str]


def require_actor(actor: object) -> str:
    """``actor`` as it is recorded, or the refusal that names the vocabulary.

    The one gate between a transport and the record, and it reads the *value*
    rather than trusting the caller's intent: ``None``, a number, a string that
    is not a word (empty, whitespace) and a word outside
    :data:`~clear_record.service.lifecycle.ACTORS` are all refused with the same
    sentence. Each is a programming error in a surface, not a client's mistake,
    and each is raised **before** the call it belongs to does anything — so a
    value the record cannot attribute a row to leaves no half-done work behind
    it and no row nothing can interpret.

    ``None`` in particular is not "no actor named": the argument itself is
    required, and a caller that passes ``None`` has named an actor that does not
    exist. Accepting it would make that call unaudited, which is exactly the
    hole this gate exists to close.
    """
    if not isinstance(actor, str) or not actor.strip() or actor not in ACTORS:
        raise ValueError(
            f"unknown actor {actor!r}; the audit record's actors are: "
            + ", ".join(ACTORS)
        )
    return actor


def row_lost(
    actor: str, action: str, target: str, outcome: str, *, reason: str
) -> None:
    """State — in the node's log — a row the record owed and did not write.

    The one thing this module does *not* promise is that every row lands: the row
    is a write of its own, and the registry can refuse it (a lock another surface
    holds past the busy timeout, a full disk). Raising the refusal would put the
    *record* between a caller and its outcome — a refused call would report a
    database error instead of the service's own answer — so the refusal is
    tolerated, and what must not be tolerated is the loss going unremarked. This
    is where it is remarked: one ``warning`` record, event :data:`ROW_LOST`,
    component ``audit``, carrying the fields the row *would* have carried
    (``actor``, ``action``, ``target``, ``outcome``) and ``reason``, the
    registry's own refusal. The fields are this module's vocabulary rather than a
    sentence of its own, so a reader of the log can line the record up with the
    rows around it and with the call it belongs to.

    The sink is :func:`clear_record.core.diagnostics.log_event`, so the record
    lands with the node's other structured records (``$CR_LOG_DIR``, rotated and
    bounded) and an unwritable sink is itself swallowed: stating a loss must never
    become a second failure.
    """
    log_event(
        "warning",
        "audit",
        ROW_LOST,
        actor=actor,
        action=action,
        target=target,
        outcome=outcome,
        reason=reason,
    )


def subject(kind: str, *fields: str) -> Callable[[Mapping[str, Any]], str]:
    """A target rule: ``kind:<the first of ``fields`` the call set>``.

    The fields are named in preference order and read off the call's own
    arguments (defaults applied). A call that set none of them — which is a
    refusal naming nothing at all — still gets a row, and it says so rather than
    guessing: ``kind:?``.
    """

    def rule(call: Mapping[str, Any]) -> str:
        for field in fields:
            value = call.get(field)
            if value is not None and value != "":
                return f"{kind}:{value}"
        return f"{kind}:?"

    return rule


def _target(rule: Target, call: Mapping[str, Any]) -> str:
    """The row's target: the rule, resolved against the call's arguments."""
    return rule(call) if callable(rule) else rule.format(**call)


def _no_row(result: Any) -> bool:
    """Whether a conditional write's answer says its statement moved no row.

    The run-lifecycle statements answer with the row they moved (or ``None``),
    or with whether they moved one (the boolean writes), so ``None`` and
    ``False`` are the statements' own "no row moved" — a lost race, a state the
    move is not legal from, or an id that names no row at all. Read rather than
    assumed: what the call answered is what the record may say about it.
    """
    return result is None or result is False


def _named_actor(
    signature: inspect.Signature, arguments: tuple[Any, ...], keywords: dict[str, Any]
) -> tuple[str | None, Mapping[str, Any]]:
    """The actor the call named — validated — and the call's own arguments.

    ``(None, {})`` when the call did not name the argument at all, or named
    something that is not an argument of the method: the method's own signature
    is what raises then, and the ``TypeError`` it raises must be the caller's
    answer, not a second one from here.

    An actor that *was* passed is run through :func:`require_actor` here — so
    ``None``, a number and an empty string are refused before the call runs and
    before any row, instead of quietly reaching the method unaudited.
    """
    try:
        bound = signature.bind(*arguments, **keywords)
    except TypeError:
        return None, {}
    if "actor" not in bound.arguments:  # a call that named none: its own TypeError
        return None, {}
    bound.apply_defaults()
    call = {name: value for name, value in bound.arguments.items() if name != "self"}
    return require_actor(bound.arguments["actor"]), call


def recorded(
    action: str, target: Target, *, conditional: bool = False
) -> Callable[[Callable[..., Any]], Any]:
    """Decorate a mutating service method so a call of it appends a row.

    Two calls append nothing, because nothing happened: a **conditional** write
    whose statement matched no row, and a key miss (see the module doc).

    ``action`` is the verb the record states (``"project.create"``), ``target``
    the rule that names what the call touched (see :data:`Target`). The method
    gains nothing to remember: the row, its outcome and its target are the
    decorator's, and the method only has to carry the ``actor`` its signature
    requires — which is what makes the record complete rather than a convention.

    ``conditional`` marks a method whose statement may match **no row** — the
    run-lifecycle writes, which return the row they moved or ``None``, or
    whether they moved one. Such a call answers with the statement's own result,
    so the decorator reads it (:func:`_no_row`) instead of assuming the call
    acted: a lost claim, a stale state or an unknown id appends **nothing**.
    That is not a refusal (the service did not decide against the move) and, for
    an id that names no row at all, there is no subject to write about — the
    ``ok`` row belongs to a write that happened, which this one did not.

    The wrapper carries the verb it was built with as ``audited_action``, so the
    inventory of what is recorded — and of what each method's rows say — is
    readable off the class itself rather than kept as a second list beside it
    (``tests/service/test_audit.py`` reads it).
    """

    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(method)

        if "actor" not in signature.parameters:
            raise TypeError(
                f"{method.__qualname__} is audited but takes no actor: the record "
                "cannot attribute a row it has no actor for"
            )

        @functools.wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            actor, call = _named_actor(signature, (self, *args), kwargs)
            if actor is None:
                # The call named none: the method's own signature is what raises
                # (`TypeError`), and there is no actor to attribute a row to.
                return method(self, *args, **kwargs)
            # Checked before the call, so an unknown actor aborts it rather than
            # being discovered half-way through the row it would write.
            actor = require_actor(actor)
            try:
                result = method(self, *args, **kwargs)
            except KeyError:
                # A key miss is a lookup that found nothing, not a decision the
                # service made: no subject to name, no row (see the module doc).
                raise
            except BaseException as exc:
                # The row is the record's, and the refusal is the caller's: the
                # append cannot replace what the call answered (see
                # `Registry.record_refusal`).
                self.record_refusal(actor, action, _target(target, call), cause=exc)
                raise
            if conditional and _no_row(result):
                # The conditional's own answer: nothing moved, so there is no
                # mutation to record. An ``ok`` row here would say the call did
                # what it was asked, which it did not.
                return result
            self.record_audit(actor, action, _target(target, call))
            return result

        # The verb this method's rows carry, on the wrapper: the audit inventory
        # is read off the class rather than restated beside it (see the docstring).
        wrapper.audited_action = action  # type: ignore[attr-defined]
        return wrapper

    return decorate


@contextlib.contextmanager
def refused_call(
    registry: Any,
    actor: str,
    action: str,
    target: str,
    *,
    refusals: tuple[type[BaseException], ...],
) -> Iterator[None]:
    """Record a service entry point's refused outcome — and only the refusals it owns.

    :func:`recorded` audits a *store* method, where the call and its row are the
    same operation. An entry point above the store — a draft write, a run's start
    — refuses for reasons of its own *before* it reaches any store call (a stale
    version, a run already in flight), and those refusals are exactly the rows an
    audit record exists for. So the entry point wraps its own guard region in this
    and names the exception types that are its **policy answers**:

    - a type in ``refusals`` — the row is appended, ``outcome='failed'``, and the
      refusal propagates unchanged;
    - anything else — including :class:`KeyError`, the key miss this record
      deliberately does not hold rows for — passes through unrecorded.

    ``actor`` is the transport's word, already through
    :func:`require_actor` by the caller's own signature; ``target`` is computed
    by the caller, because only it knows what a refused call was about.
    """
    try:
        yield
    except refusals as exc:
        registry.record_refusal(actor, action, target, cause=exc)
        raise


__all__ = [
    "FAILED",
    "OK",
    "OUTCOMES",
    "ROW_LOST",
    "Target",
    "recorded",
    "refused_call",
    "require_actor",
    "row_lost",
    "subject",
]
