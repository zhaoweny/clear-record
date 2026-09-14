"""The agent task kinds: what a task carries, and what a valid answer looks like.

The runner seam (:mod:`clear_record.service.agent`) is runtime-agnostic: it
packages a task's context, hands it to a runner, and records the artifact plus
provenance. **This** module owns the task half of that seam:

- :class:`AgentTask` — the kind, the project/meeting it belongs to, and the
  named text inputs (a transcript, a glossary snapshot, notes) an agent reasons
  over.
- :data:`TASK_KINDS` — the three structured-generation tasks of ADR-0018
  (glossary collection, transcript check, minutes).
- :data:`PIPELINES` — a **fixed** step sequence per kind. A task that needs more
  than one pass is a written-out pipeline (collect → dedupe → verify), never a
  general loop; the seam runs exactly the steps declared here and no more.
- :data:`CONTRACTS` — a machine-checkable output contract per kind. A response
  that cannot be parsed into the contract fails the task; nothing malformed is
  ever accepted, not even in part (ADR-0018).

**Nothing in this module names an agent runtime or a vendor.** A task is data;
*how* it executes is the runner's business, so a future bundled harness is a
fourth runner rather than a rewrite.

The task inputs are plain text. The contract is JSON, because that is what a
language model can be asked for and what a test can check without a model; a
fenced code block around the JSON is accepted (models emit one habitually). The
typed, validated value a contract returns is a plain JSON-shaped object, so the
artifact and its provenance serialize with no custom encoder.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping

#: The three task kinds (ADR-0018's structured generation, not agentic work).
TASK_KINDS: tuple[str, ...] = ("glossary_collection", "transcript_check", "minutes")


class AgentTaskError(Exception):
    """Base class for every agent-task failure the seam raises."""


class OutputContractError(AgentTaskError):
    """A runner's output did not satisfy the task's machine-checkable contract.

    The message names the kind and the exact defect ("missing required key
    'decisions'"), so a user can fix the prompt or the endpoint rather than
    guessing why a draft never appeared.
    """


# --- output contracts ------------------------------------------------------- #

#: A fenced code block around the whole response (```` ```json ... ``` ````).
_FENCE = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]*\n(?P<body>.*?)\n?```[ \t]*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    """The body of a single fenced block, or the text unchanged."""
    stripped = text.strip()
    match = _FENCE.match(stripped)
    return match.group("body").strip() if match else stripped


def _load_document(kind: str, text: str) -> dict:
    """Parse a runner's response as a JSON object, or fail usefully."""
    raw = _strip_fence(text)
    if not raw:
        raise OutputContractError(f"{kind}: the runner produced no output")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OutputContractError(
            f"{kind}: output is not valid JSON "
            f"({exc.msg} at line {exc.lineno} column {exc.colno})"
        ) from exc
    if not isinstance(value, dict):
        raise OutputContractError(
            f"{kind}: output must be a JSON object, got {type(value).__name__}"
        )
    return value


def _require(doc: Mapping, keys: tuple[str, ...], kind: str) -> None:
    missing = [key for key in keys if key not in doc]
    if missing:
        raise OutputContractError(
            f"{kind}: missing required key(s) {', '.join(repr(k) for k in missing)}"
        )


def _text(doc: Mapping, key: str, kind: str, where: str = "") -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OutputContractError(f"{kind}: {where}{key!r} must be a non-empty string")
    return value.strip()


def _optional_text(doc: Mapping, key: str, kind: str) -> str | None:
    value = doc.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OutputContractError(
            f"{kind}: {key!r} must be a string or null, got {type(value).__name__}"
        )
    return value.strip() or None


def _string_list(doc: Mapping, key: str, kind: str) -> list[str]:
    value = doc.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OutputContractError(f"{kind}: {key!r} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _optional_string_list(doc: Mapping, key: str, kind: str) -> list[str]:
    value = doc.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OutputContractError(
            f"{kind}: {key!r} must be a list of strings when present"
        )
    return [item.strip() for item in value if item.strip()]


class OutputContract:
    """A machine-checkable answer for one task kind.

    ``parse`` returns a validated, JSON-shaped value or raises
    :class:`OutputContractError`; there is no partial success. ``describe``
    renders the required shape for the prompt, so the request tells the model
    what the seam is going to check.
    """

    kind: str = ""

    def describe(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def parse(self, text: str) -> object:  # pragma: no cover - overridden
        raise NotImplementedError


class GlossaryCollectionContract(OutputContract):
    """Candidate terms collected from a meeting: ``{term, reading, aliases, ...}``."""

    kind = "glossary_collection"

    def describe(self) -> str:
        return (
            '{"terms": [{"term": "string (required)", "reading": "string or null", '
            '"aliases": ["string"], "definition": "string or null", '
            '"evidence": "string or null"}]}'
        )

    def parse(self, text: str) -> object:
        doc = _load_document(self.kind, text)
        _require(doc, ("terms",), self.kind)
        raw_terms = doc["terms"]
        if not isinstance(raw_terms, list):
            raise OutputContractError(f"{self.kind}: 'terms' must be a list")
        terms: list[dict] = []
        for index, item in enumerate(raw_terms):
            if not isinstance(item, Mapping):
                raise OutputContractError(
                    f"{self.kind}: terms[{index}] must be an object, "
                    f"got {type(item).__name__}"
                )
            term = item.get("term")
            if not isinstance(term, str) or not term.strip():
                raise OutputContractError(
                    f"{self.kind}: terms[{index}] is missing a non-empty 'term'"
                )
            terms.append(
                {
                    "term": term.strip(),
                    "reading": _optional_text(item, "reading", self.kind),
                    "aliases": _optional_string_list(item, "aliases", self.kind),
                    "definition": _optional_text(item, "definition", self.kind),
                    "evidence": _optional_text(item, "evidence", self.kind),
                }
            )
        return {"terms": terms}


class TranscriptCheckContract(OutputContract):
    """A corrected transcript revision plus the change list that justifies it."""

    kind = "transcript_check"

    def describe(self) -> str:
        return (
            '{"revision": "string (the corrected transcript text)", '
            '"changes": [{"before": "string", "after": "string", '
            '"reason": "string or null"}]}'
        )

    def parse(self, text: str) -> object:
        doc = _load_document(self.kind, text)
        _require(doc, ("revision", "changes"), self.kind)
        revision = _text(doc, "revision", self.kind)
        raw_changes = doc["changes"]
        if not isinstance(raw_changes, list):
            raise OutputContractError(f"{self.kind}: 'changes' must be a list")
        changes: list[dict] = []
        for index, item in enumerate(raw_changes):
            if not isinstance(item, Mapping):
                raise OutputContractError(
                    f"{self.kind}: changes[{index}] must be an object, "
                    f"got {type(item).__name__}"
                )
            changes.append(
                {
                    "before": _text(
                        item, "before", self.kind, where=f"changes[{index}] "
                    ),
                    "after": _text(
                        item, "after", self.kind, where=f"changes[{index}] "
                    ),
                    "reason": _optional_text(item, "reason", self.kind),
                }
            )
        return {"revision": revision, "changes": changes}


class MinutesContract(OutputContract):
    """A Markdown minutes body with structured front-matter as JSON fields."""

    kind = "minutes"

    def describe(self) -> str:
        return (
            '{"meeting": "string", "project": "string", "attendees": ["string"], '
            '"decisions": ["string"], "actions": ["string"], '
            '"body": "Markdown string"}'
        )

    def parse(self, text: str) -> object:
        doc = _load_document(self.kind, text)
        _require(
            doc,
            ("meeting", "project", "attendees", "decisions", "actions", "body"),
            self.kind,
        )
        return {
            "meeting": _text(doc, "meeting", self.kind),
            "project": _text(doc, "project", self.kind),
            "attendees": _string_list(doc, "attendees", self.kind),
            "decisions": _string_list(doc, "decisions", self.kind),
            "actions": _string_list(doc, "actions", self.kind),
            "body": _text(doc, "body", self.kind),
        }


CONTRACTS: dict[str, OutputContract] = {
    contract.kind: contract
    for contract in (
        GlossaryCollectionContract(),
        TranscriptCheckContract(),
        MinutesContract(),
    )
}


def contract_for(kind: str) -> OutputContract:
    """The output contract a task kind declares, or a useful error."""
    try:
        return CONTRACTS[kind]
    except KeyError:
        raise AgentTaskError(
            f"unknown task kind {kind!r}; known kinds: {', '.join(TASK_KINDS)}"
        ) from None


# --- fixed pipelines -------------------------------------------------------- #


#: One pass of a task: a name and the instructions that lead it. ``contract``
#: defaults to the kind's contract and only needs setting when a step's shape
#: differs from the artifact's (e.g. an intermediate collect step).
@dataclasses.dataclass(frozen=True)
class TaskStep:
    name: str
    instructions: str
    contract: OutputContract | None = None

    def resolved_contract(self, kind: str) -> OutputContract:
        return self.contract or contract_for(kind)


_GLOSSARY_INSTRUCTIONS = (
    "Collect the names, jargon and domain terms that this meeting's transcript "
    "spells wrongly or uses inconsistently, together with a plausible reading, "
    "aliases and the transcript evidence for each. Answer only with the JSON "
    "object the output contract requires."
)

_TRANSCRIPT_CHECK_INSTRUCTIONS = (
    "Check this meeting's transcript against the project glossary. Return the "
    "corrected transcript revision, then a change list explaining every edit. "
    "Answer only with the JSON object the output contract requires."
)

_MINUTES_INSTRUCTIONS = (
    "Write the minutes for this meeting from its record and notes: attendees, "
    "decisions and actions in the structured fields, and the Markdown minutes "
    "document in 'body'. Answer only with the JSON object the output contract "
    "requires."
)

#: The fixed pipeline per kind. One step today; a task that needs more declares
#: its steps here (collect → dedupe → verify) and the seam runs exactly those.
PIPELINES: dict[str, tuple[TaskStep, ...]] = {
    "glossary_collection": (TaskStep("collect", _GLOSSARY_INSTRUCTIONS),),
    "transcript_check": (TaskStep("check", _TRANSCRIPT_CHECK_INSTRUCTIONS),),
    "minutes": (TaskStep("draft", _MINUTES_INSTRUCTIONS),),
}


def plan_for(kind: str) -> tuple[TaskStep, ...]:
    """The fixed step sequence a task kind declares, or a useful error."""
    try:
        return PIPELINES[kind]
    except KeyError:
        raise AgentTaskError(
            f"unknown task kind {kind!r}; known kinds: {', '.join(TASK_KINDS)}"
        ) from None


# --- the task and its packaged context -------------------------------------- #


@dataclasses.dataclass(frozen=True)
class AgentTask:
    """One unit of agent work: a kind, where it belongs, and its text inputs.

    ``inputs`` are named text sections the prompt renders verbatim — the
    conventional names are ``transcript``, ``glossary``, ``record`` and
    ``notes``, but nothing enforces them: a task is data, and the seam only
    hashes and packages what it is given.
    """

    kind: str
    project: str
    meeting: str
    inputs: Mapping[str, str] = dataclasses.field(default_factory=dict)
    instructions: str = ""

    def __post_init__(self) -> None:
        if self.kind not in TASK_KINDS:
            raise AgentTaskError(
                f"unknown task kind {self.kind!r}; known kinds: {', '.join(TASK_KINDS)}"
            )
        object.__setattr__(
            self, "inputs", {str(key): str(value) for key, value in self.inputs.items()}
        )


def _canonical_json(value: object) -> str:
    """One stable rendering: sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_payload(task: AgentTask) -> dict:
    """The packaged task context, in the one order that hashes stably."""
    return {
        "kind": task.kind,
        "project": task.project,
        "meeting": task.meeting,
        "instructions": task.instructions,
        "inputs": {key: task.inputs[key] for key in sorted(task.inputs)},
    }


def context_hash(task: AgentTask) -> str:
    """The identity of a run's *inputs* — what a re-run after an edit changes.

    Editing the transcript or the glossary (or the task instructions) yields a
    new hash, so two drafts of the same task are tellable apart (ADR-0018's
    iteration hook); the same inputs in any mapping order hash the same.
    """
    payload = _canonical_json(canonical_payload(task))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_hash(steps: tuple[TaskStep, ...]) -> str:
    """The identity of the *prompt program* — what a prompt edit changes.

    It covers the step names and their instructions, not the substituted
    context, so prompt identity and input identity stay separable.
    """
    program = "\n\n".join(f"# {step.name}\n{step.instructions}" for step in steps)
    return hashlib.sha256(program.encode("utf-8")).hexdigest()


def render_prompt(
    step: TaskStep,
    payload: Mapping,
    contract: OutputContract,
    *,
    previous: str | None = None,
) -> str:
    """The exact text one runner call receives for one step.

    The instructions lead, the packaged context follows as JSON, and — for a
    later step of a multi-pass pipeline — the previous step's raw output is
    included so the pipeline chains without the seam holding any state of its
    own.
    """
    sections = [
        step.instructions.strip(),
        "## Task context\n" + json.dumps(payload, indent=2, ensure_ascii=False),
    ]
    if previous is not None:
        sections.append("## Previous step output\n" + previous)
    sections.append("## Output contract\n" + contract.describe())
    return "\n\n".join(sections) + "\n"


__all__ = [
    "AgentTask",
    "AgentTaskError",
    "CONTRACTS",
    "GlossaryCollectionContract",
    "MinutesContract",
    "OutputContract",
    "OutputContractError",
    "PIPELINES",
    "TASK_KINDS",
    "TaskStep",
    "TranscriptCheckContract",
    "canonical_payload",
    "context_hash",
    "contract_for",
    "plan_for",
    "prompt_hash",
    "render_prompt",
]
