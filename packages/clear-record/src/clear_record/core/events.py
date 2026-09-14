"""Structured progress events for the pipeline.

A stage may report progress through an optional **sink**. With no sink attached
the CLI's existing text output is the only reporting, so command-line behaviour
is unchanged; the web/service consumer attaches a sink to drive a progress bar
and an ETA, and a test attaches one to assert the sequence.

The event shape is deliberately flat and small: a stage, an optional source, a
1-based unit counter against a total, and the timing needed for an estimate.
Unit kinds are stage-specific (files for ``ingest``, chunks for ``transcribe``)
so a consumer renders one bar without knowing each stage's internals.

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
    """One progress report from a stage."""

    stage: str
    index: int = 0
    total: int = 0
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
        self._lock = threading.Lock()

    def start(self, message: str = "") -> None:
        emit(
            self.sink,
            JobEvent(stage=self.stage, index=0, total=self.total, message=message),
        )

    def advance(self, source: str | None = None, message: str = "") -> JobEvent:
        with self._lock:
            self._done += 1
            done = self._done
        elapsed = max(0.0, self._clock() - self._started)
        eta = None
        if 0 < done < self.total and elapsed > 0:
            eta = elapsed / done * (self.total - done)
        event = JobEvent(
            stage=self.stage,
            index=done,
            total=self.total,
            source=source,
            done=done >= self.total,
            elapsed_s=round(elapsed, 3),
            eta_s=round(eta, 3) if eta is not None else None,
            message=message,
        )
        emit(self.sink, event)
        return event

    def finish(self, message: str = "") -> None:
        """Emit one terminal event (for stages with no countable units)."""
        emit(
            self.sink,
            JobEvent(
                stage=self.stage,
                index=self.total,
                total=self.total,
                done=True,
                elapsed_s=round(max(0.0, self._clock() - self._started), 3),
                message=message,
            ),
        )


__all__ = ["EventSink", "JobEvent", "Level", "Progress", "emit"]
