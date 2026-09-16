"""Translatable messages: a message ID plus parameters, rendered at a boundary.

A :class:`Message` is the repo's one shape for user-facing text that must be
translated. The ID **is the English source string** (the i18n rule), and every ID
is marked with :func:`clear_record.core.i18n.deferred` so `pybabel` extracts
it. The message is *composed* where the fact is known — a provider, a resolver, a
service guard — and *rendered* where the user reads it, by the boundary's `tr`
(the console), so the emitter stays pure and the English default is byte-for-byte
stable.

A parameter may itself be a :class:`Message` (or a :class:`Joined` of them),
so a composed explanation translates as a tree rather than a half-English
sentence.

`core` is the bottom layer, so every other layer may compose and render these;
this module imports only the stdlib (ADR-0012).
"""

from __future__ import annotations

import dataclasses


def _english(msgid: str, **params: object) -> str:
    """The English renderer: the identity message ID, placeholders filled.

    It is what `str(Message)` uses so the terminal and machine surfaces stay
    English.
    """
    return msgid.format(**params) if params else msgid


@dataclasses.dataclass(frozen=True)
class Message:
    """A stable message ID plus its parameters — an explanation a boundary renders.

    The ID **is the English source string** (the i18n rule), and every ID is
    marked with :func:`clear_record.core.i18n.deferred` so `pybabel` extracts
    it. `render(translate)` fills it in the caller's locale; `str(message)`
    is the English form, so the terminal's own print and the run meta a machine
    reads are unchanged. A parameter may itself be a :class:`Message` (or a
    :class:`Joined` of them), so a composed explanation translates as a tree
    rather than a half-English sentence.

    A boundary — the console — renders it with `tr`; the emitter never calls
    `tr` itself, which keeps it pure and keeps the English default byte-for-byte
    identical.
    """

    msgid: str
    params: tuple[tuple[str, object], ...] = ()

    def render(self, translate) -> str:
        """Render in a locale, using `translate` (the boundary's `tr`)."""
        return render_message(self.as_json(), translate)

    def as_json(self) -> dict:
        """The JSON-safe form recorded in run meta (machine-read, untranslated)."""
        return {
            "id": self.msgid,
            "params": {name: _json_value(value) for name, value in self.params},
        }

    def __str__(self) -> str:
        return render_message(self.as_json(), _english)


@dataclasses.dataclass(frozen=True)
class Joined:
    """A locale-aware join of message parts (the "chose a, b and c" clause)."""

    separator: str
    parts: tuple[Message, ...]


def _json_value(value: object) -> object:
    if isinstance(value, Message):
        return value.as_json()
    if isinstance(value, Joined):
        return {
            "join": value.separator,
            "parts": [part.as_json() for part in value.parts],
        }
    return value


def render_message(node: object, translate) -> str:
    """Render a :meth:`Message.as_json` node (or a scalar) in a locale.

    Recursive so a nested explanation translates whole: a plain value passes
    through, a `{\"join\": …}` node joins its rendered parts, and a message node
    is the message ID translated with its rendered parameters. The console calls
    this with `tr` — the emitter never translates.
    """
    if not isinstance(node, dict):
        return node  # type: ignore[return-value]
    if "join" in node:
        return node["join"].join(
            render_message(part, translate) for part in node["parts"]
        )
    params = {
        name: render_message(value, translate) if isinstance(value, dict) else value
        for name, value in node["params"].items()
    }
    return translate(node["id"], **params)


__all__ = [
    "Joined",
    "Message",
    "render_message",
]
