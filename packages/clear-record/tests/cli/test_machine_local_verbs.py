"""The four machine-local verbs answer about **this** machine, never the node.

ADR-0032 makes the command line a facade of a node *for the operations a node
owns*. Four verbs sit outside that: `synth` (the fixture generator), `backends`
(the backend probe), `bench` (the benchmark) and `diagnose` (the diagnostics)
take the machine the command runs on as their subject — under a facade the
backend probe would silently answer about the node's machine instead of yours,
which is a change to what its answer is *about* rather than a refactor.

The three things pinned here are the ticket's three criteria:

* the boundary is stated where a reader meets each verb — its ``--help``;
* the probe's report opens by naming the machine it ran on;
* the guard: none of the four may reach the node, with a control proving the
  poison is the path a facade really takes.
"""

from __future__ import annotations

import platform

import pytest
from click.testing import CliRunner

from clear_record.cli import cli
from clear_record.core import node
from clear_record.core.i18n import tr

#: The verbs whose subject is the machine this command runs on.
_VERBS = ("synth", "backends", "bench", "diagnose")


def _argv(verb: str, tmp_path) -> list[str]:
    """An invocation of *verb* that does its own local work and ends.

    ``synth`` writes a two-second scene, ``bench`` and ``diagnose`` read the
    throwaway workspace/data directory they are pointed at — and none of the
    four needs a node, which is what the guard below asserts by making one
    impossible.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    if verb == "synth":
        return [
            str(tmp_path / "scene"),
            "--devices",
            "2",
            "--duration",
            "2",
            "--speakers",
            "2",
        ]
    if verb == "backends":
        return ["--all"]
    if verb == "bench":
        return ["--directory", str(workspace), "--data-dir", str(data_dir)]
    return ["--list", "--data-dir", str(data_dir)]


def test_the_machine_local_verbs_are_the_four_declared_ones() -> None:
    """The set is the four, and each is really a command of this surface."""
    assert cli.MACHINE_LOCAL_VERBS == frozenset(_VERBS)
    assert cli.MACHINE_LOCAL_VERBS <= set(cli._build_group().commands)


def test_the_facade_is_not_one_of_them() -> None:
    """`run` is the facade (ADR-0032); the machine-local set is its exception."""
    assert "run" in cli._build_group().commands
    assert "run" not in cli.MACHINE_LOCAL_VERBS


def test_the_note_names_all_four_and_says_what_the_boundary_is() -> None:
    """The sentence appended to the four help texts is the boundary, not filler."""
    note = cli.MACHINE_LOCAL_NOTE
    for verb in _VERBS:
        assert f"`{verb}`" in note, f"{verb} is missing from the boundary note"
    assert "never the node" in note
    assert "not facades" in note


@pytest.mark.parametrize("verb", _VERBS)
def test_the_boundary_is_stated_where_a_reader_meets_the_verb(verb: str) -> None:
    """Each of the four states it in its own ``--help``.

    Click re-wraps a help text to the terminal width, so the comparison is over
    whitespace-normalized text rather than the source string.
    """
    result = CliRunner().invoke(cli._build_group(), [verb, "--help"])

    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert " ".join(tr(cli.MACHINE_LOCAL_NOTE).split()) in said


def test_the_backend_probe_names_the_machine_it_ran_on() -> None:
    """The probe's answer says which machine it is about (ADR-0032).

    A report that named no machine could be read as the node's, which is exactly
    what the facade would have made of it; the line is printed before the rows
    it qualifies.
    """
    result = CliRunner().invoke(cli._build_group(), ["backends", "--all"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines, "the probe printed nothing at all"
    machine_line = lines[0]
    assert machine_line.startswith("[backends]")
    host = platform.node() or "unknown host"
    assert host in machine_line
    assert len(lines) > 1, "the machine line is the whole report"


def _poison_the_node(monkeypatch, verb: str) -> None:
    """Make every path to a node explode, so a verb that takes one fails loudly.

    The seams are the node client itself (``core.node``) and the two ways this
    surface uses it: ``ensure_node`` for the address, ``start_run``/``follow_run``
    for the run. A facade added to one of the four would go through one of them.
    """

    def _reached(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{verb} reached the node")

    for seam in ("ask", "request", "reach", "address", "recorded"):
        monkeypatch.setattr(node, seam, _reached)
    monkeypatch.setattr(cli, "ensure_node", _reached)
    monkeypatch.setattr(cli, "start_run", _reached)
    monkeypatch.setattr(cli, "follow_run", _reached)


@pytest.mark.parametrize("verb", _VERBS)
def test_a_machine_local_verb_never_reaches_the_node(
    verb: str, tmp_path, monkeypatch
) -> None:
    """The guard: give one of the four a path through the node and this fails."""
    _poison_the_node(monkeypatch, verb)

    result = CliRunner().invoke(cli._build_group(), [verb, *_argv(verb, tmp_path)])

    assert result.exit_code == 0, f"{verb}: {result.output}{result.exception!r}"


def test_the_poison_is_the_path_a_facade_really_takes(tmp_path, monkeypatch) -> None:
    """The control: ``run`` *is* a facade, and the poison above stops it.

    Without this, the guard could pass because it poisons a seam nothing uses:
    here the same seams stop the one command that must reach the node.
    """
    _poison_the_node(monkeypatch, "run")

    result = CliRunner().invoke(cli._build_group(), ["run", str(tmp_path)])

    assert result.exit_code != 0
    assert "run reached the node" in str(result.exception)
