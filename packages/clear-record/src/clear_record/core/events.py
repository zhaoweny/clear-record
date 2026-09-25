"""The pipeline's one channel: structured progress events, and the words on them.

A stage reports everything it has to say through an optional **sink**, and a
:class:`JobEvent` carries both halves of it:

- the **counters** — the stage, an optional source, a 1-based unit counter
  against a total, and the timing needed for an estimate — which a consumer
  renders as a bar and a rate. Unit kinds are stage-specific (files for
  ``ingest``, chunks for ``transcribe``) so a consumer renders one bar without
  knowing each stage's internals;
- the **words** — ``message``, exactly the text the stage wants read: a
  mid-stage line (``report_line``), a summary line, or a data item the pass
  produced. A pure progress report carries none, so a client that prints every
  non-empty message prints every word once, in the one order the stream has.

With no sink attached a stage writes only its durable log. The web/service
consumer attaches a sink to persist the same payload and drive a progress bar,
and a test attaches one to assert the sequence.

Vendor-free: stdlib only, like the rest of ``clear_record.core``.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from typing import Literal

Level = Literal["info", "warn", "error"]


@dataclasses.dataclass(frozen=True)
class JobEvent:
    """One report from a stage: its counters, and the words it has to say."""

    stage: str
    index: int = 0
    total: int = 0
    #: How many of ``index`` were **re-used from the chunk cache** rather than
    #: decoded (RUN-01/RUN-03). The two are the same unit but not the same work:
    #: a cached chunk advances the stage without a decoder ever touching it, so
    #: a reader deriving a *speed* has to subtract them — the chunks the clock
    #: in ``elapsed_s`` actually paid for are ``index - reused``.
    reused: int = 0
    source: str | None = None
    done: bool = False
    elapsed_s: float | None = None
    eta_s: float | None = None
    level: Level = "info"
    message: str = ""

    @property
    def fraction(self) -> float:
        """Completed fraction in ``[0, 1]`` (0.0 when the total is unknown)."""
        if self.total <= 0:
            return 0.0
        return min(1.0, self.index / self.total)


EventSink = Callable[[JobEvent], None]


def emit(sink: EventSink | None, event: JobEvent) -> None:
    """Send ``event`` to ``sink`` when one is attached (a no-op otherwise)."""
    if sink is not None:
        sink(event)


class Progress:
    """Counts a stage's units and emits an event per completed unit.

    ETA is a simple rate estimate (elapsed over completed units, projected onto
    the remainder); it is ``None`` until a unit completes, and on completion.
    ``advance`` is **thread-safe** because transcription's chunk pool calls it
    from worker threads.

    A unit that was **re-used from the cache** rather than decoded advances too
    — the work is done either way — but it is counted separately
    (:attr:`JobEvent.reused`), because the elapsed clock only paid for the units
    the decoder actually ran. A reader deriving "how fast is this decoding" has
    to look at ``index - reused``; a reader drawing a progress bar wants
    ``index``.
    """

    def __init__(
        self,
        stage: str,
        total: int,
        sink: EventSink | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stage = stage
        self.total = max(0, total)
        self.sink = sink
        self._clock = clock
        self._started = clock()
        self._done = 0
        self._reused = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Open the stage: one report at 0 of ``total``, and no words."""
        emit(self.sink, JobEvent(stage=self.stage, index=0, total=self.total))

    def advance(self, source: str | None = None, *, reused: bool = False) -> JobEvent:
        """One completed unit; ``reused`` when the cache supplied it.

        The count is what a speed reading needs (see the class docstring): the
        caller that advances on a **cached** chunk says so, and every event then
        carries how many of its ``index`` were served from the cache.
        """
        with self._lock:
            self._done += 1
            if reused:
                self._reused += 1
            done = self._done
            reused_count = self._reused
        elapsed = max(0.0, self._clock() - self._started)
        eta = None
        if 0 < done < self.total and elapsed > 0:
            eta = elapsed / done * (self.total - done)
        event = JobEvent(
            stage=self.stage,
            index=done,
            total=self.total,
            reused=reused_count,
            source=source,
            done=done >= self.total,
            elapsed_s=round(elapsed, 3),
            eta_s=round(eta, 3) if eta is not None else None,
        )
        emit(self.sink, event)
        return event

    def finish(self) -> None:
        """Emit one terminal event (for stages with no countable units)."""
        emit(
            self.sink,
            JobEvent(
                stage=self.stage,
                index=self.total,
                total=self.total,
                done=True,
                elapsed_s=round(max(0.0, self._clock() - self._started), 3),
            ),
        )


__all__ = ["EventSink", "JobEvent", "Level", "Progress", "emit"]
