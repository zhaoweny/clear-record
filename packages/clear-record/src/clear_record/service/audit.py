"""The audit record: who called the service, what they touched, and how it ended.

ADR-0033 decides it: **every mutating service call appends one row** —
``(at, actor, action, target, outcome)`` — to a record that is **append-only**,
and the **actor is a required argument** on the service's mutating entry points,
so no surface can forget it and no surface can forge it.

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
owns. That is the whole of it, deliberately: what the app writes for itself (the
setup marker, the external MCP client's config) is a preference, not project
data, and is not in this record. Mutating store methods are audited by
:func:`recorded`; the operations above them — a draft write, a run's start — use
:func:`refused_call` for the refusals *they* own.

:func:`recorded` is how a store method is audited. It wraps the method, so the
row is appended for every call, whatever the call did:

- the call returned — one row, ``outcome='ok'``, appended in a unit of work of
  its own, immediately after the write it names;
- the call raised — one row, ``outcome='failed'``, appended the same way. A
  refusal is the row an audit record exists for, and it *cannot* be recorded in
  the transaction it belonged to: that transaction is rolled back with the
  failure, so the row is written after it, in one of its own.

**Which refusals are rows, and which are not.** A ``failed`` row is appended
where the refusal is the *service's own policy answer*: this draft version is
stale, a run is already in flight for this meeting, this tape is not managed, the
name you gave is taken. A **key miss** is not: an unknown id is a lookup that
found nothing (:class:`KeyError`, answered as a 404 by every surface), not an
operation the service decided against — there is no subject to name, and the row
would say only that someone asked for something that does not exist. That line is
what :func:`refused_call` states at each entry point, as the exception types it
records.

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

from clear_record.service.lifecycle import ACTORS

#: How a call ended, as the record states it. Two values, and no third: a call
#: either did what it was asked or was refused.
OK = "ok"
FAILED = "failed"

#: What the row's actor did, as a term the vocabulary accepts.
OUTCOMES: tuple[str, ...] = (OK, FAILED)

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


def recorded(action: str, target: Target) -> Callable[[Callable[..., Any]], Any]:
    """Decorate a mutating service method so every call of it appends a row.

    ``action`` is the verb the record states (``"project.create"``), ``target``
    the rule that names what the call touched (see :data:`Target`). The method
    gains nothing to remember: the row, its outcome and its target are the
    decorator's, and the method only has to carry the ``actor`` its signature
    requires — which is what makes the record complete rather than a convention.
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
            except BaseException:
                self.record_audit(actor, action, _target(target, call), outcome=FAILED)
                raise
            self.record_audit(actor, action, _target(target, call))
            return result

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
    except refusals:
        registry.record_audit(actor, action, target, outcome=FAILED)
        raise


__all__ = [
    "FAILED",
    "OK",
    "OUTCOMES",
    "Target",
    "recorded",
    "refused_call",
    "require_actor",
    "subject",
]
