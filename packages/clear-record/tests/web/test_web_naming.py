"""How a client names what it wants (ADR-0032): a path is local, a model is the node's.

The node takes a **path** — a run's directory, a meeting's workspace, a tape set,
an archive root (a call's, or a project's) — only from a client that **addressed
the node itself**: the request's ``Host`` names it — its loopback, or the very
address it is listening on (``core.node``), which is what every surface here
dials. A client that reached the node through the name an operator published for
it is elsewhere, and is refused **one sentence** rather than having a path of its
own — or a same-named file on the node — acted on. Everything else is named the
way the registry names it, by id; a **model** is named neither way, because it
must already be on the node that runs the work.

The suite's ordinary client (``tests/web/conftest.py``) deliberately speaks as a
*proxied* one — ``Host: testserver``, trusted through ``CR_TRUSTED_HOSTS``, the
operator's escape hatch — which is exactly the client these routes must refuse.
So this module brings its own two: a **local** client addressing the node by its
loopback address, and a **remote** one addressing the name an operator published,
and drives both through the same real app.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clear_record.core import node
from clear_record.service import Registry, RunManager
from clear_record.web import app as web_app
from clear_record.web.app import create_app

#: The address a surface on this machine dials — ``node.NodeAddress.url`` for the
#: loopback a node binds, and the address the command line's own client uses.
LOCAL_ORIGIN = "http://127.0.0.1:8765"

#: The name an operator publishes for the console (a reverse proxy or a tailnet):
#: trusted for the request guard, and *not* loopback — so a request carrying it is
#: one this node must not take a path from.
PUBLISHED_NAME = "console.example.com"
PUBLISHED_ORIGIN = f"https://{PUBLISHED_NAME}"


def _console(
    tmp_path: Path,
    *,
    origin: str,
    trust: tuple[str, ...] = (),
    name: str = "registry",
    peer: tuple[str, int] = ("127.0.0.1", 0),
):
    """An app with a fake pipeline, reached by a client that addresses *origin*.

    The pipeline is replaced because a run is not what these tests are about; the
    HTTP edge, the addressing, the registry and the run row are all real. ``name``
    separates two consoles a single test needs, since each gets its own registry,
    and ``peer`` is the address the connection appears to come *from* — which the
    rule deliberately does not read.
    """
    registry = Registry.open(db_path=tmp_path / f"{name}.sqlite3")
    entered: list[Path] = []

    def fake_pipeline(directory, options, on_event) -> None:
        entered.append(Path(directory))
        export = Path(directory) / "export"
        export.mkdir(parents=True, exist_ok=True)
        (export / "record.md").write_text("# record\n", encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    app = create_app(registry, runs=manager, trusted_hosts=trust)
    return SimpleNamespace(
        client=TestClient(app, base_url=origin, client=peer),
        registry=registry,
        manager=manager,
        entered=entered,
    )


@pytest.fixture()
def local(tmp_path) -> SimpleNamespace:
    """A client on the node's machine: it addresses the loopback address."""
    return _console(tmp_path, origin=LOCAL_ORIGIN)


@pytest.fixture()
def remote(tmp_path) -> SimpleNamespace:
    """A client elsewhere: it addresses the name an operator published."""
    return _console(tmp_path, origin=PUBLISHED_ORIGIN, trust=(PUBLISHED_NAME,))


def _workspace(tmp_path: Path, name: str = "tapes") -> Path:
    """A directory holding one discoverable input recording."""
    workspace = tmp_path / name
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.wav").write_bytes(b"RIFF")
    return workspace


def _meeting_with_a_workspace(remote: SimpleNamespace, workspace: Path):
    """A meeting, its workspace and its tapes — registered in process.

    A remote client cannot name any of this (that is what the tests below are
    about), so the subject of the run is arranged through the registry directly,
    the way the console's own in-process flow leaves it.
    """
    remote.registry.create_project("Ops")
    meeting = remote.registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(workspace)
    )
    remote.registry.set_recording_set(meeting.id, [str(workspace / "a.wav")])
    return meeting


# --- a local client names a path, and it works ----------------------------- #
def test_a_local_client_names_a_directory_and_the_node_runs_it(local, tmp_path) -> None:
    """The whole path: one request names the directory, the node runs it."""
    workspace = _workspace(tmp_path)

    answered = local.client.post(
        "/api/runs", json={"directory": str(workspace), "model": "small"}
    )

    assert answered.status_code == 202, answered.text
    run = answered.json()["run"]
    assert run["origin"] == "api"
    # The node resolved the directory to a meeting of its own registry, whose
    # workspace *is* that directory — one translation, in one place.
    meeting = local.registry.meeting_by_id(run["meeting_id"])
    assert meeting is not None
    assert meeting.workspace_path == str(workspace.resolve())
    assert local.manager.wait(run["id"], timeout=10).status == "done"
    assert local.entered == [workspace.resolve()]


def test_a_local_client_names_a_glossary_and_the_node_decodes_with_it(
    local, tmp_path
) -> None:
    """The other half of the rule: a local client's glossary is used, and recorded.

    The glossary is a path like the directory, so the node takes it from a client
    that addressed the node — and what it used is recorded with the run (the path
    and the file's hash), so a later reader can tell one glossary from another.
    """
    workspace = _workspace(tmp_path)
    glossary = workspace / "glossary.txt"
    glossary.write_text("ZX-2000\n", encoding="utf-8")
    meeting = _meeting_with_a_workspace(local, workspace)

    answered = local.client.post(
        f"/api/meetings/{meeting.id}/runs", json={"glossary": str(glossary)}
    )

    assert answered.status_code == 202, answered.text
    run_id = answered.json()["run"]["id"]
    assert local.manager.wait(run_id, timeout=10).status == "done"
    run = local.registry.get_run(run_id)
    assert run is not None
    assert run.run_options["glossary"] == str(glossary)
    assert (run.options or {}).get("glossary_sha256")


# --- a client elsewhere is refused, with one sentence --------------------- #
def test_a_non_local_client_naming_a_directory_is_refused_with_one_sentence(
    remote, tmp_path
) -> None:
    """The headline case: a path on the wrong machine is refused, not guessed at."""
    workspace = _workspace(tmp_path)

    refused = remote.client.post("/api/runs", json={"directory": str(workspace)})

    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == web_app.PATH_IS_LOCAL
    # Nothing was resolved: no meeting was registered for a path of theirs, and
    # no run exists to have acted on a same-named directory of the node's.
    assert remote.registry.list_meetings() == []


def test_every_route_that_takes_a_path_refuses_a_non_local_client(
    remote, tmp_path
) -> None:
    """The rule holds wherever a client may name a path, not only for runs."""
    workspace = _workspace(tmp_path)
    meeting = _meeting_with_a_workspace(remote, workspace)

    refusals = {
        "a run's directory": ("post", "/api/runs", {"directory": str(workspace)}),
        "a meeting's workspace": (
            "post",
            "/api/projects/ops/meetings",
            {"title": "Elsewhere", "workspace_path": str(tmp_path / "elsewhere")},
        ),
        "a tape set": (
            "put",
            f"/api/meetings/{meeting.id}/tapes",
            {"paths": [str(workspace / "a.wav")]},
        ),
        "an archive root": (
            "post",
            f"/api/meetings/{meeting.id}/archives",
            {"root": str(tmp_path / "archive")},
        ),
        # A project's own root is where its archives are written later, so it is
        # the same path as a call's ``root``, reached by a different route.
        "a project's archive root": (
            "post",
            "/api/projects",
            {"name": "Elsewhere", "default_archive_root": str(tmp_path / "archive")},
        ),
        "a project's archive root, updated": (
            "patch",
            "/api/projects/ops",
            {"default_archive_root": str(tmp_path / "archive")},
        ),
        # A run's glossary is the *file this node decodes with*, so it is the same
        # rule as the directory beside it — on either run edge: the workspace one
        # names it in a body that also carries a path, and the registry-addressed
        # one is otherwise path-free.
        "a run's glossary": (
            "post",
            "/api/runs",
            {
                "directory": str(workspace),
                "glossary": str(workspace / "glossary.txt"),
            },
        ),
        "a meeting run's glossary": (
            "post",
            f"/api/meetings/{meeting.id}/runs",
            {"glossary": str(workspace / "glossary.txt")},
        ),
    }
    for what, (method, path, body) in refusals.items():
        answered = getattr(remote.client, method)(path, json=body)
        assert answered.status_code == 403, f"{what}: {answered.text}"
        assert answered.json()["detail"] == web_app.PATH_IS_LOCAL, what


def test_a_request_that_names_no_path_is_answered_for_any_client(
    remote, tmp_path
) -> None:
    """Only a *path* is local: a request that names none is untouched."""
    created = remote.client.post("/api/projects", json={"name": "Ops"})
    assert created.status_code == 201, created.text
    # A managed meeting names no directory — the node provisions one — and is how
    # a remote client gets a workspace at all.
    managed = remote.client.post(
        "/api/projects/ops/meetings", json={"title": "Managed", "managed": True}
    )
    assert managed.status_code == 201, managed.text

    # An empty tape set names nothing: clearing it is a registry write.
    emptied = remote.client.put(
        f"/api/meetings/{managed.json()['id']}/tapes", json={"paths": []}
    )
    assert emptied.status_code != 403, emptied.text

    # No archive root: the service's own default stands, so the refusal is about
    # the meeting's tapes rather than about where the client is.
    unrooted = remote.client.post(
        f"/api/meetings/{managed.json()['id']}/archives", json={}
    )
    assert unrooted.status_code != 403, unrooted.text

    # A project with no archive root of its own, created and updated: nothing
    # named, nothing refused.
    projectless_root = remote.client.patch("/api/projects/ops", json={"notes": "n"})
    assert projectless_root.status_code != 403, projectless_root.text


# --- the node's own address is the node, wherever it is ------------------- #
def test_the_nodes_own_address_counts_as_local(monkeypatch, tmp_path) -> None:
    """A node bound to a named address dials *that* address, and it is local.

    ``serve --host 192.168.1.5`` records and dials that address
    (``core.node``), so a client on the node's machine sends
    ``Host: 192.168.1.5:8765`` — not a loopback name, and still the very node the
    directory is on. What the app knows in process (``_served_by``, the address
    ``GET /api/node`` vouches for and the record carries) is what counts. A name
    that is *not* the node's is still refused: that is the fence.
    """
    here = node.NodeAddress(host="192.168.1.5", port=8765)
    monkeypatch.setattr(web_app, "_served_by", lambda app: here)
    at_home = _console(
        tmp_path, origin=here.url, trust=("192.168.1.5",), name="at-home"
    )
    workspace = _workspace(tmp_path)

    answered = at_home.client.post("/api/runs", json={"directory": str(workspace)})

    assert answered.status_code == 202, answered.text
    run_id = answered.json()["run"]["id"]
    assert at_home.manager.wait(run_id, timeout=10).status == "done"
    assert at_home.entered == [workspace.resolve()]

    # The same app, reached by a name that is not its own: still elsewhere.
    elsewhere = _console(
        tmp_path, origin=PUBLISHED_ORIGIN, trust=(PUBLISHED_NAME,), name="elsewhere"
    )
    refused = elsewhere.client.post("/api/runs", json={"directory": str(workspace)})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == web_app.PATH_IS_LOCAL


def test_the_peer_address_is_not_part_of_the_test(monkeypatch, tmp_path) -> None:
    """A client that is not on this machine, dialling the node's own address, is in.

    The stated consequence of deciding "local" by the name the client *addressed*
    rather than by the connection: a LAN client reaching the node at its own
    address is admitted, because ``Host`` cannot say where a client sits. It is
    not new exposure — the request guard already requires every ``Host`` to be
    loopback or named in ``CR_TRUSTED_HOSTS``, so the operator published that
    address — and the refusal still binds a client that addresses the node by
    **another** name (the test above). This pins the peer out of the decision:
    same name, a peer that is plainly elsewhere, and the path is the node's.
    """
    here = node.NodeAddress(host="192.168.1.5", port=8765)
    monkeypatch.setattr(web_app, "_served_by", lambda app: here)
    elsewhere_on_the_wire = _console(
        tmp_path,
        origin=here.url,
        trust=("192.168.1.5",),
        name="lan",
        peer=("10.9.9.9", 51234),
    )
    workspace = _workspace(tmp_path)

    answered = elsewhere_on_the_wire.client.post(
        "/api/runs", json={"directory": str(workspace)}
    )

    assert answered.status_code == 202, answered.text
    run_id = answered.json()["run"]["id"]
    assert elsewhere_on_the_wire.manager.wait(run_id, timeout=10).status == "done"
    assert elsewhere_on_the_wire.entered == [workspace.resolve()]


# --- the registry-addressed route is unchanged ---------------------------- #
def test_a_non_local_client_runs_a_registry_meeting_the_way_it_always_did(
    remote, tmp_path
) -> None:
    """A meeting is named by its id, so the same refused client may run it.

    This is the route ``PATH_IS_LOCAL`` names as the replacement, and it must
    behave exactly as it does today: the run joins the queue, writes the row and
    records the origin the caller claimed.
    """
    workspace = _workspace(tmp_path)
    meeting = _meeting_with_a_workspace(remote, workspace)

    answered = remote.client.post(f"/api/meetings/{meeting.id}/runs", json={})

    assert answered.status_code == 202, answered.text
    run = answered.json()["run"]
    assert run["origin"] == "api"
    assert remote.manager.wait(run["id"], timeout=10).status == "done"
    assert remote.entered == [workspace]


# --- a model is the node's, not a path ------------------------------------- #
def test_a_model_named_as_a_path_is_refused_on_both_run_edges(local, tmp_path) -> None:
    """A model is addressed neither by path nor by id: it is already on the node."""
    workspace = _workspace(tmp_path)
    local.registry.create_project("Ops")
    meeting = local.registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(workspace)
    )
    meetings_before = len(local.registry.list_meetings())

    edges = {
        # A directory the node has never seen: the refusal comes before it is
        # resolved, so a refused request registers no meeting for it either.
        "/api/runs": {"directory": str(tmp_path / "fresh")},
        f"/api/meetings/{meeting.id}/runs": {},
    }
    for path, body in edges.items():
        for model in (str(tmp_path / "ggml-mine.bin"), "~/models/ggml-mine.bin"):
            refused = local.client.post(path, json={**body, "model": model})
            assert refused.status_code == 400, f"{path} {model}: {refused.text}"
            assert refused.json()["detail"] == web_app.MODEL_IS_THE_NODES, path
    # Nothing ran, and nothing was written on the way to the refusal.
    assert local.manager.active_state(meeting.id) is None
    assert len(local.registry.list_meetings()) == meetings_before


# --- the rules are where a client author meets them ------------------------ #
def test_the_published_schema_states_both_rules(local) -> None:
    """``/api/docs`` is the machine surface a client author reads, so it says so."""
    schema = local.client.get("/api/openapi.json").json()
    shapes = schema["components"]["schemas"]
    assert "never a path" in shapes["RunCreate"]["description"]
    assert "local client's noun" in shapes["WorkspaceRunCreate"]["description"]
    assert "own address" in shapes["ProjectCreate"]["description"]

    routes = schema["paths"]
    directory_route = routes["/api/runs"]["post"]["description"]
    meeting_route = routes["/api/meetings/{meeting_id}/runs"]["post"]["description"]
    assert "its own address" in directory_route
    assert "the way the registry addresses it" in meeting_route
