"""The agent-task runner seam: lifecycle, provenance, review state, errors.

Every test is hermetic: the endpoint runner talks to a **local stub server**
(``127.0.0.1``, an ephemeral port, stdlib :mod:`http.server`) and the command
runner runs a **fake script** in ``tmp_path``. No network, no LLM, no key —
the only secret is a test string that must *not* survive the run.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from clear_record.service import (
    AgentConfig,
    AgentTask,
    AgentTaskError,
    CommandRunner,
    EndpointRunner,
    OutputContractError,
    RunnerError,
    RunnerOutput,
    RunnerRequest,
    TaskStep,
    accept_draft,
    read_draft,
    reject_draft,
    run_task,
)


# --- a local fake endpoint -------------------------------------------------- #


class _SilentServer(http.server.ThreadingHTTPServer):
    """A server that does not print a traceback when a client goes away."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        pass


class _FakeEndpoint:
    """A chat-completions stub that records what it was sent and replies."""

    def __init__(
        self,
        *,
        content: str | None = None,
        body: str | bytes | None = None,
        status: int = 200,
    ) -> None:
        self.content = content
        self.body = body
        self.status = status
        self.requests: list[tuple[dict[str, str], dict]] = []
        self._lock = threading.Lock()
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                with endpoint._lock:
                    endpoint.requests.append(
                        (dict(self.headers.items()), json.loads(raw))
                    )
                payload = endpoint._payload()
                self.send_response(endpoint.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:  # noqa: D102
                pass

        self._server = _SilentServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _payload(self) -> bytes:
        if self.body is not None:
            return (
                self.body.encode("utf-8") if isinstance(self.body, str) else self.body
            )
        document = {"choices": [{"message": {"content": self.content}}]}
        return json.dumps(document).encode("utf-8")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __enter__(self) -> _FakeEndpoint:
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def header(self, index: int, name: str) -> str | None:
        for key, value in self.requests[index][0].items():
            if key.lower() == name.lower():
                return value
        return None


def _glossary(answer: str = "Falcon") -> str:
    """A valid glossary-collection answer as the model would emit it."""
    return json.dumps(
        {
            "terms": [
                {
                    "term": answer,
                    "reading": "FAL-kun",
                    "aliases": [answer.lower()],
                    "definition": "a tracked object",
                    "evidence": "00:00:03 [mic] the falcon is up",
                }
            ]
        }
    )


def _task(**overrides) -> AgentTask:
    values = {
        "kind": "glossary_collection",
        "project": "ops",
        "meeting": "kickoff",
        "inputs": {"transcript": "the falcon is up", "glossary": "Falcon\n"},
    }
    values.update(overrides)
    return AgentTask(**values)


def _drafts(root: Path) -> list[Path]:
    """The draft records under ``root`` — written only when a run succeeds."""
    return sorted(root.rglob("run.json"))


# --- the endpoint runner ---------------------------------------------------- #


def test_endpoint_run_produces_a_draft_with_full_provenance(tmp_path: Path) -> None:
    with _FakeEndpoint(content=_glossary()) as endpoint:
        runner = EndpointRunner(endpoint.url, model="test-model")
        draft = run_task(_task(), tmp_path, runner=runner)

    assert draft.review_state == "draft"
    assert draft.value == {
        "terms": [
            {
                "term": "Falcon",
                "reading": "FAL-kun",
                "aliases": ["falcon"],
                "definition": "a tracked object",
                "evidence": "00:00:03 [mic] the falcon is up",
            }
        ]
    }
    # The artifact and its provenance are real files.
    assert draft.artifact_path.is_file()
    assert json.loads(draft.artifact_path.read_text(encoding="utf-8")) == draft.value
    assert draft.provenance_path.is_file()
    assert draft.run_dir.is_dir()

    provenance = draft.provenance
    assert provenance.kind == "glossary_collection"
    assert provenance.project == "ops"
    assert provenance.meeting == "kickoff"
    assert provenance.runner == "endpoint"
    assert provenance.model == "test-model"
    assert len(provenance.context_hash) == 64
    assert len(provenance.prompt_hash) == 64
    assert provenance.steps == ("collect",)
    assert provenance.started_at and provenance.ended_at
    assert provenance.run_id in draft.run_dir.name

    # The request carried the packaged context and the contract.
    _headers, sent = endpoint.requests[0]
    assert sent["model"] == "test-model"
    prompt = sent["messages"][-1]["content"]
    assert "the falcon is up" in prompt
    assert "Output contract" in prompt


def test_accept_and_reject_are_explicit_and_persist(tmp_path: Path) -> None:
    with _FakeEndpoint(content=_glossary()) as endpoint:
        draft = run_task(_task(), tmp_path, runner=EndpointRunner(endpoint.url))

    assert not draft.accepted and not draft.rejected
    accepted = accept_draft(draft)
    assert accepted.review_state == "accepted"
    assert accepted.accepted
    assert read_draft(draft.run_dir).review_state == "accepted"

    rejected = reject_draft(read_draft(draft.run_dir))
    assert rejected.rejected
    assert read_draft(draft.run_dir).review_state == "rejected"


def test_the_credential_is_sent_from_the_environment_and_never_stored(
    tmp_path: Path,
) -> None:
    secret = "sk-test-not-a-real-key"
    environ = {"CR_AGENT_API_KEY": secret}
    with _FakeEndpoint(content=_glossary()) as endpoint:
        runner = EndpointRunner(
            endpoint.url, api_key_env="CR_AGENT_API_KEY", environ=environ
        )
        draft = run_task(_task(), tmp_path, runner=runner)

    # It *was* used: the endpoint saw it on the wire.
    assert endpoint.header(0, "Authorization") == f"Bearer {secret}"
    # It was not persisted anywhere: artifact, provenance or packaged input.
    for path in (
        draft.artifact_path,
        draft.provenance_path,
        draft.run_dir / "input.json",
    ):
        assert secret not in path.read_text(encoding="utf-8")

    # And the resolved config carries only the variable *name*.
    config = AgentConfig(
        endpoint=endpoint.url, model="m", api_key_env="CR_AGENT_API_KEY"
    )
    assert config.api_key_env == "CR_AGENT_API_KEY"
    assert secret not in json.dumps(config.__dict__)


def test_a_local_endpoint_needs_no_key(tmp_path: Path) -> None:
    with _FakeEndpoint(content=_glossary()) as endpoint:
        run_task(_task(), tmp_path, runner=EndpointRunner(endpoint.url))

    assert endpoint.header(0, "Authorization") is None


def test_a_named_key_that_is_unset_fails_closed(tmp_path: Path) -> None:
    with _FakeEndpoint(content=_glossary()) as endpoint:
        runner = EndpointRunner(
            endpoint.url, api_key_env="CR_AGENT_API_KEY", environ={}
        )
        with pytest.raises(RunnerError, match="refusing to call unauthenticated"):
            run_task(_task(), tmp_path, runner=runner)

    assert endpoint.requests == []  # nothing was sent
    assert _drafts(tmp_path) == []  # and no draft was written


@pytest.mark.parametrize(
    ("endpoint_kwargs", "match"),
    [
        ({"status": 500, "content": "boom"}, "HTTP 500"),
        ({"body": "not json at all"}, "non-JSON body"),
        ({"body": json.dumps({"choices": []})}, "no choices"),
        ({"content": ""}, "empty completion"),
    ],
    ids=["http-500", "non-json", "no-choices", "empty"],
)
def test_endpoint_errors_are_useful_and_write_no_draft(
    tmp_path: Path, endpoint_kwargs: dict, match: str
) -> None:
    with _FakeEndpoint(**endpoint_kwargs) as endpoint:
        with pytest.raises(RunnerError, match=match):
            run_task(_task(), tmp_path, runner=EndpointRunner(endpoint.url))
    assert _drafts(tmp_path) == []


def test_a_fenced_json_answer_is_accepted(tmp_path: Path) -> None:
    fenced = "```json\n" + _glossary("Booster") + "\n```"
    with _FakeEndpoint(content=fenced) as endpoint:
        draft = run_task(_task(), tmp_path, runner=EndpointRunner(endpoint.url))
    assert draft.value["terms"][0]["term"] == "Booster"


# --- the command runner ----------------------------------------------------- #

_FAKE_AGENT = """\
import json
import pathlib
import sys

args = dict(zip(sys.argv[1:], sys.argv[2:]))
payload = json.loads(pathlib.Path(args["--input"]).read_text(encoding="utf-8"))
prompt = pathlib.Path(args["--prompt"]).read_text(encoding="utf-8")
pathlib.Path("receipt.json").write_text(
    json.dumps(
        {
            "kind": payload["kind"],
            "project": args["--project"],
            "meeting": args["--meeting"],
            "saw_contract": "Output contract" in prompt,
        }
    ),
    encoding="utf-8",
)
pathlib.Path(args["--out"]).write_text(
    json.dumps({"terms": [{"term": "Falcon"}]}), encoding="utf-8"
)
"""


def _write_script(tmp_path: Path, body: str = _FAKE_AGENT) -> Path:
    script = tmp_path / "fake_agent.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def _template(script: Path) -> str:
    return (
        f"{sys.executable} {script} --input {{input_json}} --out {{output_file}} "
        "--project {project} --meeting {meeting} --prompt {prompt_file}"
    )


def test_command_runner_substitutes_every_placeholder(tmp_path: Path) -> None:
    script = _write_script(tmp_path)
    draft = run_task(
        _task(), tmp_path / "runs", runner=CommandRunner(_template(script))
    )

    assert draft.provenance.runner == "command"
    assert draft.value["terms"][0]["term"] == "Falcon"
    receipt = json.loads((draft.run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt == {
        "kind": "glossary_collection",
        "project": "ops",
        "meeting": "kickoff",
        "saw_contract": True,
    }
    # {prompt_file} and {input_json} were written for the command to read.
    assert (draft.run_dir / "prompt.txt").is_file()
    assert (draft.run_dir / "input.json").is_file()


def test_an_unknown_placeholder_is_refused_at_construction() -> None:
    with pytest.raises(AgentTaskError, match="unknown placeholder"):
        CommandRunner("agent --wat {nope} --out {output_file}")


def _fake_completed(**overrides) -> subprocess.CompletedProcess:
    values = {"args": ["cmd"], "returncode": 0, "stdout": "", "stderr": ""}
    values.update(overrides)
    return subprocess.CompletedProcess(**values)


def test_a_command_that_fails_writes_no_draft(tmp_path: Path) -> None:
    runner = CommandRunner(
        "agent --out {output_file}",
        runner=lambda *a, **k: _fake_completed(returncode=2, stderr="agent exploded"),
    )
    with pytest.raises(RunnerError, match="exited 2"):
        run_task(_task(), tmp_path, runner=runner)
    assert _drafts(tmp_path) == []


def test_a_command_that_writes_no_output_fails(tmp_path: Path) -> None:
    runner = CommandRunner(
        "agent --out {output_file}", runner=lambda *a, **k: _fake_completed()
    )
    with pytest.raises(RunnerError, match="no output file"):
        run_task(_task(), tmp_path, runner=runner)


def test_a_command_that_writes_an_empty_output_fails(tmp_path: Path) -> None:
    def write_empty(argv, **kwargs):
        Path(argv[argv.index("--out") + 1]).write_text("   ", encoding="utf-8")
        return _fake_completed()

    runner = CommandRunner("agent --out {output_file}", runner=write_empty)
    with pytest.raises(RunnerError, match="empty output file"):
        run_task(_task(), tmp_path, runner=runner)


# --- contracts, pipelines and the seam's runtime-agnosticism ---------------- #


def test_malformed_output_fails_the_task_without_a_partial_draft(
    tmp_path: Path,
) -> None:
    with _FakeEndpoint(content="{not valid json") as endpoint:
        with pytest.raises(OutputContractError, match="not valid JSON"):
            run_task(_task(), tmp_path, runner=EndpointRunner(endpoint.url))
    assert _drafts(tmp_path) == []


class _ScriptedRunner:
    """A runner that returns canned answers and records every request."""

    kind = "scripted"

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.requests: list[RunnerRequest] = []

    def run(self, request: RunnerRequest) -> RunnerOutput:
        self.requests.append(request)
        return RunnerOutput(text=self.answers.pop(0), model="canned")


def test_a_task_runs_its_fixed_pipeline_step_by_step(tmp_path: Path) -> None:
    collect = json.dumps({"terms": [{"term": "Falcon"}, {"term": "Falcon"}]})
    dedupe = json.dumps({"terms": [{"term": "Falcon"}]})
    runner = _ScriptedRunner([collect, dedupe])
    plan = (
        TaskStep("collect", "Collect the terms."),
        TaskStep("dedupe", "Drop the duplicates."),
    )

    draft = run_task(_task(), tmp_path, runner=runner, plan=plan)

    assert [request.step for request in runner.requests] == ["collect", "dedupe"]
    # The second step's prompt chains the first step's raw output.
    assert collect in runner.requests[1].prompt
    assert draft.provenance.steps == ("collect", "dedupe")
    assert draft.value == {
        "terms": [
            {
                "term": "Falcon",
                "reading": None,
                "aliases": [],
                "definition": None,
                "evidence": None,
            }
        ]
    }
    # The seam never assumes the runner: this one is neither endpoint nor command.
    assert draft.provenance.runner == "scripted"


def test_run_task_builds_its_runner_from_config(tmp_path: Path) -> None:
    with _FakeEndpoint(content=_glossary()) as endpoint:
        config = AgentConfig(endpoint=endpoint.url, model="configured")
        draft = run_task(_task(), tmp_path, config=config)
    assert draft.provenance.model == "configured"


def test_run_task_without_any_config_says_what_to_set(tmp_path: Path) -> None:
    with pytest.raises(AgentTaskError, match="no agent configured"):
        run_task(_task(), tmp_path, config=AgentConfig())
