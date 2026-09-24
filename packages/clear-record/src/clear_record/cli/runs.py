"""A run the command line starts on the node: submit it, follow it, report it.

ADR-0032 makes a run started from the command line a **node run**. The command
line is a client of the recorded node (:mod:`clear_record.core.node`): the node
owns the queue and the registry row, and what is left for this surface is to
speak to it — name the workspace in one request, follow the run the node answers
with, and render what the node reports. Nothing here runs a stage: the pipeline
stays where the node runs it.

What this deliberately leaves alone, because it is the node's own work:

* the stage's **words** — the run's stream carries *progress* for a client today
  (the stage, and the counters its closing event reported), so the follow loop
  prints progress and invents no prose;
* what a client may **set** — a run request carries only the fields the node's
  run API declares, and ``cli.cli`` refuses a flag it cannot carry rather than
  dropping it.

Layering (ADR-0004/ADR-0012): this is command surface, so it imports ``core`` and
nothing above it. The node is reached over HTTP with the one client every surface
asks through, never through ``service`` — the row a run writes is written by the
node, which is what makes the origin it records trustworthy.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from typing import Any

from clear_record.core import node

#: Where a client starts a run over a workspace **directory** on the node.
RUNS_PATH = "/api/runs"
#: One run's row and its live state (the shape the JSON API serves).
RUN_PATH = "/api/runs/{run_id}"
#: One page of a run's event stream, read from a cursor.
EVENTS_PATH = "/api/runs/{run_id}/events?after={after}"
#: Seconds between two reads of a run that is still in flight. The console polls
#: its own fragment at one second; a client watching a multi-hour run has no
#: reason to ask more often than that.
POLL_S = 1.0

#: The surface name this client records on the run it starts, in the node's own
#: vocabulary (``service.lifecycle.RUN_ORIGINS``). It is spelled here because the
#: command surface may not import ``service`` (ADR-0004/ADR-0012) and the wire
#: value has to be written by the client — so a test ties this constant to the
#: service's declaration, and the node refuses a value it does not know.
CLI_ORIGIN = "cli"


class Refused(RuntimeError):
    """The node refused the request, in the node's own words.

    The sentence is the node's (the JSON API's ``detail``) and is never composed
    here: the same refusal has to read the same to every client, and the service
    owns the message (``docs/i18n.md``).
    """


@dataclasses.dataclass(frozen=True)
class Started:
    """What the node answered with: the run it accepted, and whose run it is."""

    run_id: int
    meeting_id: int


@dataclasses.dataclass(frozen=True)
class Progress:
    """One read of a run on the node, in the fields a follower renders.

    The JSON API's summary (``service.runs.RunSummary``) read into what this
    surface needs: where the run is, which stage its stream last reported and
    against how many units, the run's own error, and the two questions a follower
    must not answer for itself — whether it has ended (``terminal``) and whether
    it **succeeded** (``succeeded``), both derived from the lifecycle by the
    service that declares it.
    """

    status: str
    stage: str | None
    index: int
    total: int
    error: str | None
    terminal: bool
    succeeded: bool


def start(target: node.NodeAddress, directory: str, body: dict[str, Any]) -> Started:
    """Ask the node to run *directory*, and return the run it accepted.

    ``body`` is the run request in the **node's** own field names (what
    ``/api/runs`` declares), and the directory is added to it here: the one field
    that makes this a run over a workspace rather than over a registry id.
    :class:`Refused` carries the node's sentence for anything but a 2xx — a run
    already in flight for this workspace, an unknown flag, a workspace this node
    cannot run.
    """
    answer = node.request(target, "POST", RUNS_PATH, dict(body, directory=directory))
    if not answer.ok:
        raise Refused(answer.detail())
    run = answer.body["run"]
    return Started(run_id=int(run["id"]), meeting_id=int(run["meeting_id"]))


def read(target: node.NodeAddress, run_id: int) -> Progress:
    """One read of a run's state on the node."""
    answer = node.request(target, "GET", RUN_PATH.format(run_id=run_id))
    if not answer.ok:
        raise Refused(answer.detail())
    state = answer.body["state"]
    return Progress(
        status=state["status"],
        stage=state["stage"],
        index=int(state["index"]),
        total=int(state["total"]),
        error=state["error"],
        terminal=bool(state["terminal"]),
        succeeded=bool(state["succeeded"]),
    )


def events(
    target: node.NodeAddress, run_id: int, after: int
) -> tuple[list[dict[str, Any]], int]:
    """One page of a run's event stream, and the cursor to continue from."""
    answer = node.request(target, "GET", EVENTS_PATH.format(run_id=run_id, after=after))
    if not answer.ok:
        raise Refused(answer.detail())
    return list(answer.body["events"]), int(answer.body["next"])


def follow(
    target: node.NodeAddress,
    run_id: int,
    *,
    out: Callable[[str], None] = print,
    poll: float = POLL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Progress:
    """Follow a run on the node, printing each stage as the run closes it.

    What is printed is what the run's own stream carries for a client today: one
    line per stage the run finished — the stage, and the counters its **closing**
    event reported — which is progress, the same material the console's run row
    draws from the same stream. The stage's own words and the data items it
    returns are the channel's own work; nothing here invents them.

    A stage is printed once, when its first closing event lands: a stage's line
    that belongs to a chunk copies that chunk's event wholesale (``report_line``
    with ``report=``), so the last chunk's line carries ``done`` as well and would
    otherwise repeat the stage's own line.

    Returns the run's terminal state, so the caller reports the outcome and says
    where the record went.
    """
    cursor = 0
    reported: set[str] = set()
    while True:
        page, cursor = events(target, run_id, cursor)
        for event in page:
            stage = str(event["stage"])
            if event.get("done") and stage not in reported:
                reported.add(stage)
                out(f"[{stage}] {event['index']} / {event['total']}")
        state = read(target, run_id)
        if state.terminal:
            return state
        sleep(poll)


__all__ = [
    "CLI_ORIGIN",
    "EVENTS_PATH",
    "POLL_S",
    "RUNS_PATH",
    "RUN_PATH",
    "Progress",
    "Refused",
    "Started",
    "events",
    "follow",
    "read",
    "start",
]
