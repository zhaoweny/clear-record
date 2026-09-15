"""Task kinds, their output contracts, context hashing and agent config.

The runner seam's other half: what a task *is* (a kind plus named text inputs),
what a valid answer looks like (a machine-checkable contract), and the plumbing
that points the seam at an endpoint or a command template. Everything here is
pure except the config file reads, which use ``tmp_path`` and an explicit
``environ`` so no host state leaks in.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from clear_record.service import (
    CONTRACTS,
    TASK_INPUTS,
    TASK_KINDS,
    AgentConfig,
    AgentConfigError,
    AgentTask,
    AgentTaskError,
    CommandRunner,
    EndpointRunner,
    OutputContractError,
    TaskStep,
    build_task,
    canonical_payload,
    context_hash,
    contract_for,
    glossary_collection_task,
    load_agent_config,
    minutes_task,
    plan_for,
    prompt_hash,
    render_prompt,
    transcript_check_task,
)
from clear_record.service import agent as agent_module

# --- output contracts ------------------------------------------------------- #


def test_every_task_kind_declares_a_contract_and_a_pipeline() -> None:
    assert set(CONTRACTS) == set(TASK_KINDS)
    for kind in TASK_KINDS:
        assert contract_for(kind).kind == kind
        assert plan_for(kind)  # a non-empty fixed pipeline
        assert contract_for(kind).describe()


def test_an_unknown_kind_is_a_useful_error() -> None:
    with pytest.raises(AgentTaskError, match="unknown task kind"):
        contract_for("summarize")
    with pytest.raises(AgentTaskError, match="unknown task kind"):
        plan_for("summarize")


def test_glossary_collection_contract_normalizes_optional_fields() -> None:
    value = contract_for("glossary_collection").parse(
        json.dumps({"terms": [{"term": " Falcon ", "aliases": ["falcon", " "]}]})
    )
    assert value == {
        "terms": [
            {
                "term": "Falcon",
                "reading": None,
                "aliases": ["falcon"],
                "definition": None,
                "evidence": None,
            }
        ]
    }


@pytest.mark.parametrize(
    ("kind", "text", "match"),
    [
        ("glossary_collection", json.dumps({}), "missing required key"),
        ("glossary_collection", json.dumps({"terms": {}}), "'terms' must be a list"),
        (
            "glossary_collection",
            json.dumps({"terms": [{"reading": "x"}]}),
            r"terms\[0\] is missing",
        ),
        ("transcript_check", json.dumps({"changes": []}), "missing required key"),
        (
            "transcript_check",
            json.dumps({"revision": "ok", "changes": [{"after": "b"}]}),
            r"changes\[0\]",
        ),
        ("minutes", json.dumps({"meeting": "m"}), "missing required key"),
        (
            "minutes",
            json.dumps(
                {
                    "meeting": "m",
                    "project": "p",
                    "attendees": "nope",
                    "decisions": [],
                    "actions": [],
                    "body": "b",
                }
            ),
            "'attendees' must be a list",
        ),
        ("minutes", "[]", "must be a JSON object"),
        ("minutes", "   ", "produced no output"),
    ],
    ids=[
        "glossary-missing",
        "glossary-not-list",
        "glossary-term",
        "check-missing",
        "check-change",
        "minutes-missing",
        "minutes-attendees",
        "not-an-object",
        "empty",
    ],
)
def test_a_malformed_answer_names_the_exact_defect(
    kind: str, text: str, match: str
) -> None:
    with pytest.raises(OutputContractError, match=match):
        contract_for(kind).parse(text)


def test_a_wrapped_object_is_recovered_but_still_validated() -> None:
    """A chat model wraps the object in prose; recover it, but keep the schema."""
    document = {
        "meeting": "m",
        "project": "p",
        "attendees": ["A"],
        "decisions": [],
        "actions": [],
        "body": "# Minutes",
    }
    payload = json.dumps(document)

    prose_then_json = contract_for("minutes").parse("Here are the minutes:\n" + payload)
    json_then_prose = contract_for("minutes").parse(
        payload + "\nLet me know if you want changes."
    )
    assert prose_then_json["body"] == "# Minutes"
    assert json_then_prose["body"] == "# Minutes"

    # Recovery must not weaken the contract: the same wrapping around an object
    # that breaks the schema still fails.
    with pytest.raises(OutputContractError, match="must be a list"):
        contract_for("minutes").parse(
            "Here you go:\n" + json.dumps({**document, "attendees": "nope"})
        )


def test_each_contract_returns_a_validated_json_shape() -> None:
    check = contract_for("transcript_check").parse(
        json.dumps(
            {
                "revision": "corrected",
                "changes": [{"before": "a", "after": "b", "reason": "spelling"}],
            }
        )
    )
    assert check == {
        "revision": "corrected",
        "changes": [{"before": "a", "after": "b", "reason": "spelling"}],
    }

    minutes = contract_for("minutes").parse(
        json.dumps(
            {
                "meeting": "Kickoff",
                "project": "Ops",
                "attendees": ["Ada"],
                "decisions": ["ship it"],
                "actions": ["write docs"],
                "body": "# Kickoff",
            }
        )
    )
    assert minutes["actions"] == ["write docs"]
    assert minutes["body"] == "# Kickoff"


# --- context hashing -------------------------------------------------------- #


def _task(inputs: dict, **overrides) -> AgentTask:
    values = {"kind": "glossary_collection", "project": "ops", "meeting": "kickoff"}
    values.update(overrides)
    return AgentTask(inputs=inputs, **values)


def test_context_hash_is_order_independent_and_input_sensitive() -> None:
    first = _task({"transcript": "a", "glossary": "b"})
    shuffled = _task({"glossary": "b", "transcript": "a"})
    edited = _task({"transcript": "a-edited", "glossary": "b"})

    assert context_hash(first) == context_hash(shuffled)
    assert context_hash(first) != context_hash(edited)
    assert list(canonical_payload(first)["inputs"]) == ["glossary", "transcript"]


def test_instructions_change_the_context_hash_but_not_vice_versa() -> None:
    base = _task({"transcript": "a"})
    with_instructions = _task({"transcript": "a"}, instructions="be terse")
    assert context_hash(base) != context_hash(with_instructions)


def test_prompt_hash_covers_the_prompt_program_not_the_context() -> None:
    plan = plan_for("glossary_collection")
    assert prompt_hash(plan) == prompt_hash(plan_for("glossary_collection"))

    edited = (TaskStep("collect", "A different instruction."),)
    assert prompt_hash(plan) != prompt_hash(edited)

    # Two tasks with different inputs, same prompt program: prompt hash matches.
    other_context = _task({"transcript": "different"})
    assert context_hash(_task({"transcript": "a"})) != context_hash(other_context)
    assert prompt_hash(plan) == prompt_hash(plan_for(other_context.kind))


def test_render_prompt_carries_context_and_chains_a_previous_step() -> None:
    task = _task({"transcript": "the falcon is up"})
    payload = canonical_payload(task)
    step = plan_for(task.kind)[0]
    contract = step.resolved_contract(task.kind)

    first = render_prompt(step, payload, contract)
    assert "the falcon is up" in first
    assert contract.describe() in first
    assert "Previous step output" not in first

    second = render_prompt(step, payload, contract, previous="previous answer")
    assert "previous answer" in second


def test_a_task_rejects_an_unknown_kind_up_front() -> None:
    with pytest.raises(AgentTaskError, match="unknown task kind"):
        AgentTask(kind="summarize", project="p", meeting="m")


# --- the per-kind prompts --------------------------------------------------- #


@pytest.mark.parametrize(
    ("kind", "tokens"),
    [
        (
            "glossary_collection",
            ("transcript", "glossary", "evidence", "aliases", "correct spelling"),
        ),
        (
            "transcript_check",
            ("transcript", "glossary", "context", "revision", "changes"),
        ),
        (
            "minutes",
            ("transcript", "glossary", "context", "attendees", "decisions", "actions"),
        ),
    ],
)
def test_each_kind_leads_with_its_own_prompt(
    kind: str, tokens: tuple[str, ...]
) -> None:
    plan = plan_for(kind)
    assert len(plan) == 1  # one coherent structured generation
    instructions = plan[0].instructions
    for token in tokens:
        assert token in instructions
    assert "Answer only with the JSON object" in instructions


def test_the_three_kinds_do_not_share_a_prompt() -> None:
    prompts = {kind: plan_for(kind)[0].instructions for kind in TASK_KINDS}
    assert len(set(prompts.values())) == len(TASK_KINDS)


def test_a_prompt_does_not_name_a_runtime_or_a_model() -> None:
    """Prompts are data: no vendor, endpoint or bundled harness may appear."""
    banned = ("openai", "ollama", "lm studio", "llama.cpp", "gpt", "claude")
    for kind in TASK_KINDS:
        text = plan_for(kind)[0].instructions.lower()
        assert not any(name in text for name in banned)


# --- the task builders ------------------------------------------------------ #


def test_builders_package_the_sections_each_kind_declares() -> None:
    tasks = {
        "glossary_collection": glossary_collection_task(
            "ops", "kickoff", transcript="t", glossary="Falcon\n"
        ),
        "transcript_check": transcript_check_task(
            "ops", "kickoff", transcript="t", glossary="Falcon\n", context="c"
        ),
        "minutes": minutes_task(
            "ops", "kickoff", transcript="t", glossary="Falcon\n", context="c"
        ),
    }
    for kind, task in tasks.items():
        assert task.kind == kind
        assert task.project == "ops"
        assert task.meeting == "kickoff"
        assert set(task.inputs) == set(TASK_INPUTS[kind])


def test_build_task_refuses_a_missing_section_and_an_unknown_kind() -> None:
    with pytest.raises(AgentTaskError, match="missing context section"):
        build_task("minutes", "ops", "kickoff", {"transcript": "t"})
    with pytest.raises(AgentTaskError, match="unknown task kind"):
        build_task("summarize", "ops", "kickoff", {})


def test_build_task_passes_an_extra_section_through() -> None:
    task = build_task(
        "minutes",
        "ops",
        "kickoff",
        {"transcript": "t", "glossary": "", "context": "", "notes": "side note"},
    )
    assert task.inputs["notes"] == "side note"


def test_editing_a_packaged_section_changes_the_context_hash() -> None:
    base = glossary_collection_task("ops", "kickoff", transcript="a", glossary="G")
    edited = glossary_collection_task("ops", "kickoff", transcript="b", glossary="G")
    assert context_hash(base) != context_hash(edited)


def test_a_packaged_task_renders_its_sections_and_contract() -> None:
    task = minutes_task(
        "Ops",
        "Kickoff",
        transcript="00:00:03 [mic] the falcon is up",
        glossary="Falcon\n",
        context="meeting: Kickoff",
    )
    step = plan_for("minutes")[0]
    prompt = render_prompt(
        step, canonical_payload(task), step.resolved_contract("minutes")
    )
    for token in ("Kickoff", "Falcon", "attendees"):
        assert token in prompt


# --- agent config ----------------------------------------------------------- #

_CONFIG = """\
[agent]
endpoint = "http://config.test/v1"
model = "config-model"
api_key_env = "CONFIG_KEY"
timeout = 30

[agent.commands]
transcript_check = "check --in {input_json} --out {output_file}"
"""


def test_config_file_is_parsed_into_plumbing(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_CONFIG, encoding="utf-8")

    resolved = load_agent_config(environ={}, config_file=config)

    assert resolved.endpoint == "http://config.test/v1"
    assert resolved.model == "config-model"
    assert resolved.api_key_env == "CONFIG_KEY"
    assert resolved.timeout == 30.0
    assert resolved.commands == {
        "transcript_check": "check --in {input_json} --out {output_file}"
    }
    assert resolved.ok


def test_env_beats_the_config_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    env = {
        "CR_AGENT_ENDPOINT": "http://env.test/v1",
        "CR_AGENT_MODEL": "env-model",
        "CR_AGENT_TIMEOUT": "5",
    }

    resolved = load_agent_config(environ=env, config_file=config)

    assert resolved.endpoint == "http://env.test/v1"
    assert resolved.model == "env-model"
    assert resolved.timeout == 5.0
    # Commands are file-only plumbing and survive the env override.
    assert resolved.commands["transcript_check"].startswith("check ")


def test_a_command_template_wins_over_the_endpoint() -> None:
    config = AgentConfig(
        endpoint="http://config.test/v1",
        commands={"minutes": "agent minutes --in {input_json}"},
    )
    command = config.runner_for("minutes")
    assert isinstance(command, CommandRunner)
    assert isinstance(config.runner_for("glossary_collection"), EndpointRunner)


def test_no_config_at_all_names_the_two_ways_to_configure() -> None:
    with pytest.raises(AgentConfigError, match="no agent configured"):
        AgentConfig().runner_for("minutes")


def test_a_bad_placeholder_is_surfaced_and_skipped(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[agent.commands]\nminutes = "agent --wat {nope}"\n', encoding="utf-8"
    )

    resolved = load_agent_config(environ={}, config_file=config)

    assert "minutes" not in resolved.commands
    assert any("unknown placeholder" in problem for problem in resolved.problems)
    assert not resolved.ok
    assert "unknown placeholder" in capsys.readouterr().err


def test_an_unknown_kind_in_commands_is_surfaced(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[agent.commands]\nsummarize = "agent {input_json}"\n', encoding="utf-8"
    )
    resolved = load_agent_config(environ={}, config_file=config)
    assert resolved.commands == {}
    assert any("unknown task kind" in problem for problem in resolved.problems)


def test_a_non_positive_timeout_is_surfaced_and_defaulted(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[agent]\ntimeout = -1\n", encoding="utf-8")
    resolved = load_agent_config(environ={}, config_file=config)
    assert resolved.timeout == 120.0
    assert any("not a positive number" in problem for problem in resolved.problems)


def test_a_malformed_config_is_surfaced_but_never_raises(
    tmp_path: Path, capsys
) -> None:
    broken = tmp_path / "config.toml"
    broken.write_text("this is not toml = = =", encoding="utf-8")

    resolved = load_agent_config(environ={}, config_file=broken)

    assert resolved.endpoint is None
    assert any("not valid TOML" in problem for problem in resolved.problems)
    assert "clear-record: agent config:" in capsys.readouterr().err


def test_the_resolved_config_never_carries_a_key_value() -> None:
    secret = "sk-hosted-secret"
    env = {
        "CR_AGENT_ENDPOINT": "http://hosted.test/v1",
        "CR_AGENT_API_KEY_ENV": "OPENAI_API_KEY",
        "OPENAI_API_KEY": secret,
    }

    resolved = load_agent_config(environ=env, config_file=None)

    # Only the *name* of the variable is stored.
    assert resolved.api_key_env == "OPENAI_API_KEY"
    persisted = json.dumps(dataclasses.asdict(resolved))
    assert secret not in persisted
    assert secret not in repr(resolved)


def test_the_default_config_is_cached_and_resettable(monkeypatch) -> None:
    agent_module.reset_default_config()
    try:
        first = agent_module.default_config()
        assert agent_module.default_config() is first
    finally:
        agent_module.reset_default_config()
    assert agent_module.default_config() is not first


def test_a_config_that_raises_is_reported_rather_than_raised(
    monkeypatch, capsys
) -> None:
    def boom(**kwargs):
        raise RuntimeError("no environment")

    monkeypatch.setattr(agent_module, "load_agent_config", boom)
    agent_module.reset_default_config()
    try:
        config = agent_module.default_config()
        assert not config.ok
        assert any("agent disabled" in problem for problem in config.problems)
    finally:
        agent_module.reset_default_config()
    assert "agent disabled" in capsys.readouterr().err
