"""The run lifecycle's own seam: the declaration, and the copies that must not exist.

A run's state is read by the persistence layer, the manager, the console and the
agent surface, so the states were once spelled in five places and a new one cost
five classifications. These tests defend the four properties that make the one
declaration worth having: the declaration is *internally* complete (every move
names states that exist, and no state is declared that nothing enters), its
**announcements** are the moves' own names rather than a vocabulary a receiver
could subscribe to and never receive, the **actors** each move declares are the
ones that perform it, and no second list of its states exists anywhere in the
tree.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from clear_record.service import EMITTED_EVENTS
from clear_record.service.lifecycle import (
    ANNOUNCEMENTS,
    CLAIM,
    ENQUEUE,
    FAIL,
    FINISH,
    INTERRUPT,
    QUEUE,
    RUN_ORIGINS,
    RUN_STATUSES,
    STOP_QUEUED,
    STOP_RUNNING,
    TRANSITIONS,
)

#: The shipped tree the copy guard reads: a *source* scan, because the defect it
#: looks for is a reader writing the states out again, which no behaviour shows.
#: ``parents[2]`` is ``packages/clear-record`` (this file is ``tests/service/…``).
_PACKAGE = Path(__file__).resolve().parents[2]
_SOURCE = _PACKAGE / "src" / "clear_record"
_TEMPLATES = _SOURCE / "web" / "templates"

#: The module that **is** the declaration: a collection of run states inside it is
#: the vocabulary, not a copy of it. Exempt by resolved path, never by filename —
#: a second module called ``lifecycle.py`` is a copy like any other, which a
#: basename exemption would let through.
_DECLARATION = _SOURCE / "service" / "lifecycle.py"

#: A vocabulary that legitimately names run-state words and is not the run's: the
#: meeting's own lifecycle names ``running``, ``failed`` and ``interrupted`` too —
#: it describes the meeting, and the run's word for the same moment is a different
#: fact — so the constant declaring it is exempt where it is **declared**. The
#: exemption is by module *and* name, as :data:`_DECLARATION`'s is by module alone:
#: a copy a reader writes under the same name in another module is still a copy of
#: the run's states, which an exemption by name anywhere would let through.
_SHARED_WORDS = {_SOURCE / "service" / "models.py": frozenset({"MEETING_STATUSES"})}

#: How many run-state words in one literal make it a copy of the classification
#: rather than a phrase about a single run. Two, because one state word belongs to
#: vocabularies this tree already has beside the run's — an archive's
#: ``tr("failed")``, the agent panel's ``tr("not running")`` — and would be a
#: false report, and because the duplication this ticket removes is a *pair* or a
#: longer list every time.
_COPY_THRESHOLD = 2

#: The calls the templates' user-facing text arrives through, and the literal each
#: is given. Both quote characters, and the literal may begin on the next line:
#: the templates use ``tr('…')`` inside HTML attributes as well as ``tr("…")``, and
#: a call may be wrapped — a guard that read only the double-quoted one-line form
#: would miss the other spelling of the same defect. Only these calls are read: a
#: template's markup and its ``{{ run.status }}`` values are the row's data, not a
#: classification.
_TEMPLATE_TEXT = re.compile(
    r"""\b(?:tr|trn|deferred)\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""",
    re.DOTALL,
)

#: Files the scan must have read for its verdict to mean anything: a moved root
#: would otherwise make it pass over an empty set, which is how a guard silently
#: stops guarding. The first two pin the Python root, the third the templates'.
_SCANNED = ("store.py", "app.py", "activity.html")

_WORDS = re.compile(r"[a-z_]+")


def _moves_from(state: str) -> list[str]:
    return [move.name for move in TRANSITIONS if state in move.sources]


def test_every_move_names_states_that_exist() -> None:
    """A move's sources and target are the declared vocabulary, not a typo.

    The registry's conditional statements are built from these values
    (``claim_run`` moves what ``CLAIM`` declares, and so on), so a misspelled
    target would be a statement that can never match — a run that looks claimed
    and never executes, with no error anywhere.
    """
    for move in TRANSITIONS:
        assert set(move.sources) <= set(RUN_STATUSES), move
        assert move.target in RUN_STATUSES, move


def test_every_declared_state_is_entered_by_a_move() -> None:
    """No state is declared that nothing can reach.

    A state nothing enters is a state no query can ever return: it reads like a
    supported outcome while being unreachable, which is what a half-done edit
    looks like when a state is added to the vocabulary and its move is not.
    """
    entered = {move.target for move in TRANSITIONS}
    assert entered == set(RUN_STATUSES)


def test_one_move_creates_a_run_and_it_carries_no_source() -> None:
    """A run begins exactly once, and beginning is the move with no prior state.

    ``sources`` is empty only for the move that writes the row's first status, so
    the two readings of an empty tuple — "creates" and "moved from nothing" — are
    the same reading.
    """
    creators = [move for move in TRANSITIONS if not move.sources]
    assert [move.name for move in creators] == ["enqueued"]
    assert creators[0].target in RUN_STATUSES


def test_only_a_move_that_announces_is_in_the_receivers_vocabulary() -> None:
    """A receiver subscribes to what the moves publish, and nothing else.

    ``EMITTED_EVENTS`` is what a user configures a webhook against, so a name in
    it that no move raises is a subscription that can never fire, and a move that
    announces outside it is a notification nobody can ask for. The run half of
    that vocabulary is derived from the moves, which is what this pins.
    """
    announced = tuple(move.event for move in TRANSITIONS if move.announced)
    silent = [move.event for move in TRANSITIONS if not move.announced]
    assert ANNOUNCEMENTS == announced
    assert all(name in EMITTED_EVENTS for name in announced)
    assert not any(name in EMITTED_EVENTS for name in silent)


def test_every_move_leaves_a_state_a_run_can_be_in() -> None:
    """Every state a move starts from is a state a run can be *in*.

    The registry's statements read ``sources`` as a predicate, so a source that is
    not a state a row can hold would be a statement that matches no row at all —
    the claim, the stop or the reap silently doing nothing.
    """
    for move in TRANSITIONS:
        for state in move.sources:
            assert _moves_from(state), (
                f"nothing enters {state!r}, but {move.name} leaves it"
            )


def test_the_surfaces_that_may_ask_for_a_move_are_the_ones_that_do() -> None:
    """Each move's declared actors are the actor that performs it.

    ``actors`` is what makes "which transitions are legal from which origin" part
    of the declaration rather than a comment, so it needs one reader: this table,
    read off the call sites. The surface half is enforced in the code — the origin
    vocabulary *is* :data:`ENQUEUE`'s actors, and both start paths validate an
    origin against it — while the queue's half names the manager, which is the
    only writer that claims, ends or reaps a run.
    """
    performed_by = {
        # RunManager.start / Registry.create_run, by a console, API, MCP or CLI caller
        ENQUEUE: RUN_ORIGINS,
        # Registry.claim_run, from the drain's thread
        CLAIM: (QUEUE,),
        # RunManager._run_pipeline, when the pipeline returns
        FINISH: (QUEUE,),
        # the drain's handler, the quarantine (_refuse_run) and _fail_unrunnable
        FAIL: (QUEUE,),
        # RunManager.cancel, asked for by a surface
        STOP_QUEUED: RUN_ORIGINS,
        # RunManager._finish_stopped, the owner honouring its own cancel
        STOP_RUNNING: (QUEUE,),
        # RunManager._reap_dead_runs, the node reaping an owner that died
        INTERRUPT: (QUEUE,),
    }
    assert {move: move.actors for move in TRANSITIONS} == performed_by


def _python_copies(path: Path) -> list[str]:
    """Every literal collection in one module that names two or more states.

    Collections and dict *keys*, since a table keyed by states — the console's
    label table is one — is a copy in the same sense as a tuple. Mixed literals
    report the states among them, so a list that grew a non-state element is still
    one copy of the pair.
    """

    def values(node: ast.AST) -> list[str]:
        elements = node.keys + node.values if isinstance(node, ast.Dict) else node.elts
        return [
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]

    tree = ast.parse(path.read_text(encoding="utf-8"))
    # A vocabulary declared as another domain's is not a copy of the run's; the
    # exemption is the module that declares it *and* the name it declares it under,
    # so its words stay its own and the same name elsewhere does not.
    shared = _SHARED_WORDS.get(path, frozenset())
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = {node.target.id}
        else:
            continue
        if names & shared and node.value is not None:
            exempt.add(id(node.value))

    found: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if id(child) in exempt:
                continue
            if isinstance(child, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
                states = sorted(
                    {value for value in values(child) if value in RUN_STATUSES}
                )
                if len(states) >= _COPY_THRESHOLD:
                    found.append(
                        f"{path.relative_to(_PACKAGE)}:{child.lineno} {tuple(states)}"
                    )
                    continue  # a copy nested inside another copy is one report
            walk(child)

    walk(tree)
    return found


def _template_copies(path: Path) -> list[str]:
    """Every user-facing template string that names two or more states.

    Read over the whole file rather than line by line, because the literal a call
    is given may start on the line after it; the line number reported is the one
    the call starts on.
    """
    text = path.read_text(encoding="utf-8")
    found: list[str] = []
    for match in _TEMPLATE_TEXT.finditer(text):
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        states = sorted(
            {word for word in _WORDS.findall(literal) if word in RUN_STATUSES}
        )
        if len(states) >= _COPY_THRESHOLD:
            lineno = text.count("\n", 0, match.start()) + 1
            found.append(
                f"{path.relative_to(_PACKAGE)}:{lineno} {tuple(states)} {literal!r}"
            )
    return found


def test_no_module_but_the_lifecycle_spells_the_states_out_again() -> None:
    """A hand-copied list of states is the defect the lifecycle module ends.

    The states used to be a tuple in the values module, a hand-copied terminal
    tuple beside it, a pair in the console's label table and another pair in the
    chip, so adding a state was a hunt and the site that was missed failed in
    silence — a status no query lists is a run nothing sees. This keeps the *copy*
    class from landing again.

    What it reads, exactly: every ``*.py`` under ``src/clear_record`` except the
    declaring module (:data:`_DECLARATION`, by resolved path), and every ``*.html``
    under ``src/clear_record/web/templates``. In Python it reads literal
    collections — tuple, list, set and dict (keys and values) — and in a template
    the string literals of ``tr``/``trn``/``deferred`` calls; a literal that names
    **two or more** run states is reported, mixed literals included.

    What it deliberately does not read, and why:

    * **A single state word in a phrase.** Other vocabularies legitimately use the
      same words — an archive is ``tr("failed")``, an agent panel's endpoint is
      ``tr("not running")`` — so one word is a phrase and two is a copy. The
      meeting's own lifecycle is exempt where it is declared
      (:data:`_SHARED_WORDS`): that module, under that name.
    * **``tests/``.** A test's literal is often the *independent* side of a
      comparison rather than a repetition: ``test_store.py`` writes the frozen
      index predicate out on purpose, so that the revision's ``WHERE`` is compared
      with something the declaration did not produce.
    * **Revision ``0009``'s SQL.** Its predicate is one string, not a collection,
      and it *has* to exist: a revision states its own DDL. It is pinned by
      ``tests/service/test_store.py``, which reads the built index back and
      compares it with the declaration — a stronger check than a scan.
    * **``frontend/``.** The stylesheet's status comment is a list of CSS classes,
      not shipped logic, and the compiled assets have their own freshness recipe
      (``just web-assets-check``).
    """
    modules = [
        path for path in sorted(_SOURCE.rglob("*.py")) if path != _DECLARATION
    ] + sorted(_TEMPLATES.rglob("*.html"))
    assert {path.name for path in modules} >= set(_SCANNED), (
        f"the scan read {len(modules)} files and did not find {_SCANNED}: "
        f"its roots ({_SOURCE}, {_TEMPLATES}) are not the shipped tree"
    )
    offenders = [
        entry
        for path in modules
        for entry in (
            _python_copies(path) if path.suffix == ".py" else _template_copies(path)
        )
    ]
    assert offenders == []
