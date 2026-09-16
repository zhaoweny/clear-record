"""Explicit re-run scoping: which chunks a re-run is allowed to touch.

A *re-run scope* narrows a transcription pass to part of the work — one or more
sources, and/or a time range — so the tuning loop (glossary ↔ transcript) can
apply an edited glossary without re-decoding a multi-hour tape end to end.

Why an **explicit** scope, and not an inferred one
--------------------------------------------------

The glossary is fed to the decoder as its *initial prompt*, so in principle it
can change **any** chunk's output. There is therefore no per-chunk rule that is
sound by itself: a chunk whose cached transcript does not mention a term may
still be the chunk that term was added to fix (the decoder wrote something else
entirely, which is usually *why* the term was added). Deciding by "does the
transcript look like the term" would skip exactly the chunk that motivated the
edit, and silently ship a stale transcript.

So the mechanism is a scope the caller **states**: a source and/or a range. A
scoped run reuses every chunk outside the scope from the cache, whatever
glossary those chunks were decoded under, and records that fact per chunk so a
later unscoped run still re-decodes them. The conservative near-match guard in
``clear_record.engine.text`` is a *widening* on top of the scope: it can only
add chunks to the re-decode set, never remove one, so it cannot reintroduce the
silent-stale failure.

An unparseable range, a source not in the manifest, or a scope that selects no
chunk at all is an :class:`ScopeError` — never a silent full re-decode and never
a silent no-op.

The DAG edge stays ``cli → core``: this module is stdlib-only like the rest of
``clear_record.core``.
"""

from __future__ import annotations

import dataclasses
import re

from clear_record.core.i18n import tr


class ScopeError(ValueError):
    """A re-run scope that cannot be honoured as written.

    Callers turn this into an actionable message (the CLI into a usage error or
    a ``SystemExit``); it is never swallowed into a wider re-decode.
    """


#: ``H:MM``, ``H:MM:SS`` or plain seconds. Two fields are read as hours:minutes
#: (the shape a multi-hour tape is named in: ``12:30-18:00``); use plain seconds
#: for a sub-minute range.
_CLOCK_RE = re.compile(r"^\d+(?::\d{1,2}(?:\.\d+)?){0,2}$")


def _clock(token: str, whole: str) -> float:
    """Seconds for one end of a range, or :class:`ScopeError`."""
    token = token.strip()
    if not _CLOCK_RE.match(token):
        raise ScopeError(
            tr(
                "cannot read {token!r} in re-run range {whole!r}: expected "
                "H:MM, H:MM:SS or seconds (e.g. 12:30-18:00).",
                token=token,
                whole=whole,
            )
        )
    fields = [float(part) for part in token.split(":")]
    if len(fields) == 1:
        return fields[0]
    if len(fields) == 2:
        hours, minutes = fields
        if minutes >= 60:
            raise ScopeError(
                tr(
                    "minutes must be under 60 in {whole!r} ({token!r}).",
                    whole=whole,
                    token=token,
                )
            )
        return hours * 3600.0 + minutes * 60.0
    hours, minutes, seconds = fields
    if minutes >= 60 or seconds >= 60:
        raise ScopeError(
            tr(
                "minutes/seconds must be under 60 in {whole!r} ({token!r}).",
                whole=whole,
                token=token,
            )
        )
    return hours * 3600.0 + minutes * 60.0 + seconds


def parse_time_range(text: str) -> tuple[float, float]:
    """Parse ``START-END`` into seconds, or raise :class:`ScopeError`.

    Both ends are required, so "from 12:30 on" cannot be mistaken for a complete
    scope; a half-specified range must fail rather than quietly mean everything.
    """
    whole = (text or "").strip()
    if not whole:
        raise ScopeError(tr("re-run range is empty."))
    parts = whole.split("-")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ScopeError(
            tr(
                "cannot read re-run range {whole!r}: expected START-END "
                "(e.g. 12:30-18:00 or 745-1050).",
                whole=whole,
            )
        )
    start = _clock(parts[0], whole)
    end = _clock(parts[1], whole)
    if end <= start:
        raise ScopeError(
            tr(
                "re-run range {whole!r} ends at or before it starts; the end "
                "must be later than the start.",
                whole=whole,
            )
        )
    return start, end


def format_seconds(seconds: float) -> str:
    """``H:MM:SS`` for a scope description (whole seconds; no sub-second noise)."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


@dataclasses.dataclass(frozen=True)
class ChunkScope:
    """The part of a run a re-run is allowed to re-decode.

    ``sources`` empty means "every source"; ``start_s``/``end_s`` of ``None``
    means "the whole timeline". Both empty is not a scope — :meth:`parse`
    returns ``None`` instead, which is what keeps "no scope" and "an empty
    scope" from being confused at the call site.
    """

    sources: tuple[str, ...] = ()
    start_s: float | None = None
    end_s: float | None = None

    @property
    def is_empty(self) -> bool:
        return not self.sources and self.start_s is None

    def selects_source(self, source_id: str) -> bool:
        """True when ``source_id`` is inside the source part of the scope."""
        return not self.sources or source_id in self.sources

    def selects_chunk(self, start_s: float, end_s: float) -> bool:
        """True when a chunk's span *overlaps* the range part of the scope.

        Overlap, not containment: a chunk that starts before the range and runs
        into it must be re-decoded, because the range's audio is inside it.
        """
        if self.start_s is None or self.end_s is None:
            return True
        return end_s > self.start_s and start_s < self.end_s

    def selects(self, source_id: str, start_s: float, end_s: float) -> bool:
        """True when this chunk of this source is inside the scope."""
        return self.selects_source(source_id) and self.selects_chunk(start_s, end_s)

    @classmethod
    def parse(
        cls,
        sources: tuple[str, ...] | list[str] | None = None,
        time_range: str | None = None,
    ) -> ChunkScope | None:
        """Build a scope from raw run inputs, or ``None`` when none was given.

        Names are stripped and de-duplicated, order preserved. A blank name is
        dropped rather than matching nothing; a malformed range is a
        :class:`ScopeError`, because a typo silently meaning "everything" is the
        failure this whole module exists to prevent.
        """
        names = tuple(
            dict.fromkeys(
                name.strip() for name in (sources or ()) if name and name.strip()
            )
        )
        start = end = None
        if time_range and time_range.strip():
            start, end = parse_time_range(time_range)
        if not names and start is None:
            return None
        return cls(sources=names, start_s=start, end_s=end)

    def describe(self) -> str:
        """A short human description for logs and run meta."""
        bits: list[str] = []
        if self.sources:
            bits.append("sources=" + ",".join(self.sources))
        if self.start_s is not None and self.end_s is not None:
            bits.append(
                f"range={format_seconds(self.start_s)}-{format_seconds(self.end_s)}"
            )
        return " ".join(bits) or "all chunks"


__all__ = [
    "ChunkScope",
    "ScopeError",
    "format_seconds",
    "parse_time_range",
]
