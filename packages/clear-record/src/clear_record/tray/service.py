"""Supervise a node — the one this process started, or one it only joined.

Deliberately **Qt-free**: the tray icon is a thin shell over this, so the
supervision logic (ensure a node, wait-until-ready, live state, stop, restart) is
testable without a display.

**The tray attaches first.** :meth:`ServiceController.start` resolves the recorded
address and completes one request against it
(:func:`clear_record.core.node.ask`), exactly as the command line and the MCP
adapter do, so a node that is already up is the node this tray becomes a client of
— no second node is started, and the ``--host``/``--port`` the tray was given are
not bound. Only when nothing answers is a node started here, **embedded in this
process** — that is the tray's own posture, and it is why the controller owns the
thread. That node records the address it bound where the surfaces resolve it
(:mod:`clear_record.core.node`), exactly as ``clear-record serve`` does. A node
that *is* its own process needs no such controller: the headless node's supervisor
is ``clear-record serve --supervise``, which restarts the server inside the node's
own process (:func:`clear_record.web.app.serve`).

So the controller is a **client of a node**, and that node is either one it
**joined** or one it **started**. Everything it reports is that node's:
:meth:`ServiceController.state` is the health probe's answer, asked through the
same one client the command line and the MCP adapter use, on the same path they
reach it by — "healthy" says the node answered as a surface's request reaches it,
and RUNNING versus UNREACHABLE is the node's own answer whether this tray started
it or joined it (a node it only joined has no thread here at all). The one thing
this process contributes is whether it has a node of its own, which is what tells
a tray that has stopped its node from a node that is merely unreachable. What is
this process's is also the node it started: :meth:`ServiceController.stop` and
:meth:`ServiceController.restart` act on that one, and a node the tray only joined
is left running, because the user did not ask this tray to own it.
"""

from __future__ import annotations

import enum
import threading

from clear_record.core import node
from clear_record.service import Registry
from clear_record.web.app import NodeServer, create_app


class ServiceState(enum.StrEnum):
    """What the node this tray is a client of is doing **right now**.

    ``STOPPED`` — there is no node this tray is a client of: none was ever
    started or joined, or the node this process started has been stopped.
    ``RUNNING`` — that node answers its health path. ``UNREACHABLE`` — that node
    does not answer (still starting, wedged, or gone), whether this tray started
    it or joined it.
    """

    STOPPED = "stopped"
    RUNNING = "running"
    UNREACHABLE = "unreachable"


class ServiceController:
    """A client of the node, and the supervisor of the one it starts."""

    def __init__(
        self,
        *,
        host: str = node.DEFAULT_HOST,
        port: int = node.DEFAULT_PORT,
        data_dir: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self._server = None
        self._thread: threading.Thread | None = None
        #: The node this tray is a client of **without owning it**: the recorded
        #: address, proved by one request (:meth:`start`). It is ``None`` while
        #: this controller supervises a node of its own, and while it is a client
        #: of nothing at all — the handles below tell those two apart.
        self._joined: node.NodeAddress | None = None

    @property
    def address(self) -> node.NodeAddress:
        """Where the node this tray is a client of answers.

        The **bound socket** is the truth for the node this controller started: it
        names the port that node really holds (``--port 0`` included), and unlike
        the record it cannot belong to another node. A node the tray only
        **joined** is named by the record it answered on — that is the address
        every surface resolves, and it is not re-read here: a node started later
        overwrites the file, which would hand this controller an address it never
        proved. The requested ``host:port`` stands while there is neither, which
        is also the honest answer for a port we never bound (the server died,
        e.g. the port was taken: nothing answers there).
        """
        server = self._server
        bound = server.bound() if isinstance(server, NodeServer) else None
        if bound is not None:
            return bound
        if self._joined is not None:
            return self._joined
        return node.NodeAddress(self.host, self.port)

    @property
    def url(self) -> str:
        return self.address.url

    @property
    def running(self) -> bool:
        """Whether the server this controller started is still on its thread."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def supervises(self) -> bool:
        """Whether the node this tray is a client of is one **it** started.

        ``False`` for a node it only joined — that node is not this process's to
        stop or restart — and ``False`` before :meth:`start`, when the tray is a
        client of nothing yet.
        """
        return self._joined is None and (
            self._server is not None or self._thread is not None
        )

    def offers_restart(self, *, state: ServiceState | None = None) -> bool:
        """Whether the menu's Restart item can act right now.

        True for the node this tray started — restart is its act — and for the one
        case where the tray owns no node and the node it **joined** has stopped:
        then a click *starts* a node of its own (:meth:`restart_or_start`), the
        only way back for a tray whose joined node went away. A joined node that
        still answers is not this tray's to restart, so nothing is offered there.

        A caller that has already read the live state — the poll tick reads it once
        and shows it in the status line — passes it as ``state`` rather than paying
        a second health request.
        """
        if self.supervises:
            return True
        live = self.state() if state is None else state
        return live is ServiceState.UNREACHABLE

    def start(self) -> None:
        """Become a client of a node: join the one that answers, or start one.

        The **attach path first** — the recorded address is resolved and proved
        by one request (:func:`clear_record.core.node.ask`) — so a node already up
        is used as every other surface uses it, and no second node is started.
        Only when nothing answers is a node started here, on a thread of this
        process, on the ``--host``/``--port`` this controller was given; an
        address that already answers is never taken over.

        Already a client? Nothing is started twice: the call is a no-op while the
        node this process started is on its thread, and a node it joined is left
        where it is.
        """
        if self.running:
            return
        try:
            self._joined = node.ask()
        except node.NoNodeError:
            self._joined = None
        if self._joined is not None:
            return

        import uvicorn

        app = create_app(Registry.open(data_dir=self.data_dir))
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning"
        )
        # NodeServer records the bound address while it listens, so this posture
        # publishes its address exactly as `clear-record serve` does.
        self._server = NodeServer(config)
        app.state.server = self._server
        self._thread = threading.Thread(
            target=self._server.run, name="cr-console", daemon=True
        )
        self._thread.start()

    def wait_until_ready(self, timeout: float = 15.0, interval: float = 0.1) -> bool:
        """Poll the node's health until it answers or ``timeout`` is up."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            if self._thread is not None and not self._thread.is_alive():
                return False  # the node this process started died (e.g. port taken)
            time.sleep(interval)
        return self.healthy()

    def healthy(self) -> bool:
        """Does the node's health URL answer exactly 200, right now?

        Reached through the one node client
        (:func:`clear_record.core.node.reach`) the surfaces reach the node with,
        so the tray asks the node the same question they ask — of the node it
        joined as much as of the one it started.
        Only a 200 counts and redirects are **not** followed: a 3xx (e.g. a
        redirect to the setup page) is not a healthy server. The path stays the
        current one — B3 replaces it with a dedicated credential-free
        ``GET /health`` (AUTH-06/AUTH-07), whose response must not carry the
        registry path.
        """
        try:
            node.reach(self.address)
        except node.NoNodeError:
            return False
        return True

    def state(self) -> ServiceState:
        """The live state of the node this tray is a client of, probed now.

        The node's own answer *is* the state: ``RUNNING`` when it answers its
        health path, ``UNREACHABLE`` when it does not — whichever node it is, one
        this tray started or one it joined and does not own. ``STOPPED`` is the one
        thing no probe can report — it is *no node this tray is a client of*:
        nothing started or joined yet, or the node this process started has been
        stopped. The handles are read for exactly that question and no other, so a
        node the tray joined (which has no thread here at all) is never reported
        as stopped while it answers.

        The tray reads this on a timer, so it must stay cheap and must not cache,
        and the probe has to remain answerable without a credential (see the
        health route).
        """
        if self._joined is None and self._server is None and self._thread is None:
            return ServiceState.STOPPED
        return ServiceState.RUNNING if self.healthy() else ServiceState.UNREACHABLE

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the node **this tray started**, and wait; True when it is gone.

        A node it only **joined** is not this tray's to stop: quitting leaves that
        node running with its record as it is, and there is nothing here to wait
        for. Stopping one of its own is a graceful shutdown, which waits for
        in-flight requests (a transcription takes a while), so the join may time
        out. The handles are then **kept**: clearing them would report "stopped" —
        and let :meth:`start` bind a second server on the port the first still
        holds. A later :meth:`stop` retries the join.
        """
        if self._joined is not None:
            self._joined = None
            return True
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is None:
            self._server = None
            return True
        self._thread.join(timeout)
        if self._thread.is_alive():
            return False
        self._thread = None
        self._server = None
        return True

    def restart(self, timeout: float = 15.0) -> bool:
        """Stop the node this tray started, then start one again.

        A node the tray only **joined** is not restarted from here: ``False`` is
        answered and nothing is started, because the tray serves the node it found
        — restarting that node is its owner's act (``serve``, or the tray that
        started it), and starting a second one is what the join avoids.

        For a node of its own, ``False`` means the old server did not shut down in
        time: nothing is started (a second bind on the same port would fail), the
        handles stay, and the caller can report the failure and retry.
        """
        if self._joined is not None:
            return False
        if not self.stop(timeout):
            return False
        self.start()
        return self.wait_until_ready(timeout=timeout)

    def restart_or_start(self, timeout: float = 15.0) -> bool:
        """What the menu's Restart item does for the node this tray is a client of.

        The node this tray **started** is restarted (:meth:`restart`). A tray that
        owns no node starts one exactly as :meth:`start` does — the record is
        re-read and the node that answers is joined, or one is started when none
        answers — which is the way back after the node a tray joined has stopped.
        It happens because the user **clicked**, never on the poll tick that saw
        the node gone (:meth:`offers_restart` is what enables the item).
        """
        if self.supervises:
            return self.restart(timeout)
        self.start()
        return self.wait_until_ready(timeout=timeout)


__all__ = ["ServiceController", "ServiceState"]
