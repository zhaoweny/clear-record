"""The structured progress seam: counting, timing, and thread safety.

The `Progress` helper is tested directly (a fake clock makes the ETA
deterministic); stage-level wiring is covered in `tests/pipeline/test_progress_events`.
"""

from __future__ import annotations

import threading

from clear_record.core import JobEvent, Progress


class _Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_progress_counts_and_estimates() -> None:
    clock = _Clock()
    events: list[JobEvent] = []
    progress = Progress("transcribe", 4, events.append, clock=clock)

    progress.start()
    assert events[-1].index == 0
    assert events[-1].total == 4
    assert not events[-1].done

    clock.t = 2.0
    first = progress.advance(source="a")
    assert (first.index, first.total, first.source) == (1, 4, "a")
    assert first.elapsed_s == 2.0
    assert first.eta_s == 6.0  # 2 s per unit, 3 units left
    assert not first.done
    assert first.fraction == 0.25

    clock.t = 4.0
    progress.advance(source="a")
    clock.t = 6.0
    progress.advance(source="b")
    clock.t = 8.0
    last = progress.advance(source="b")

    assert last.index == 4 and last.done and last.eta_s is None
    assert last.fraction == 1.0
    assert [event.index for event in events] == [0, 1, 2, 3, 4]


def test_advance_counts_reused_units_separately() -> None:
    """A cached unit advances the stage but is not work the clock paid for.

    The count is cumulative and rides every event, so a reader deriving a speed
    can subtract it: ``index`` is the progress bar's business, ``index - reused``
    is the decoder's (RUN-03).
    """
    events: list[JobEvent] = []
    progress = Progress("transcribe", 4, events.append)

    progress.advance(source="a", reused=True)
    progress.advance(source="a", reused=True)
    decoded = progress.advance(source="a")

    assert (decoded.index, decoded.reused) == (3, 2)
    assert [event.reused for event in events] == [1, 2, 2]
    assert decoded.index - decoded.reused == 1  # one chunk the decoder ran


def test_finish_marks_a_stageless_run_done() -> None:
    events: list[JobEvent] = []
    progress = Progress("transcribe", 0, events.append, clock=_Clock())
    progress.start()
    progress.finish()
    assert events[-1].done
    assert events[-1].index == 0 and events[-1].total == 0


def test_no_sink_is_a_noop() -> None:
    progress = Progress("ingest", 3, None, clock=_Clock())
    progress.start()
    progress.advance()
    progress.finish()  # must not raise


def test_advance_is_thread_safe() -> None:
    """Concurrent chunk workers must not lose or duplicate an index."""
    events: list[JobEvent] = []
    progress = Progress("transcribe", 100, events.append, clock=_Clock())
    threads = [
        threading.Thread(target=lambda: [progress.advance() for _ in range(10)])
        for _ in range(10)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(event.index for event in events) == list(range(1, 101))
