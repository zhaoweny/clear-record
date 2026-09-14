"""The three task kinds end to end: a packaged task → a contract-valid draft.

The runner seam's own lifecycle (transport, credentials, review states, error
paths) is covered in ``test_agent_runner.py``; this module covers the **kinds**
built on top of it. Each kind is driven with the real fixed pipeline
(:func:`~clear_record.service.plan_for`) through a **local stub endpoint** and,
for one kind, a **fake command script** — no network, no LLM, no key. The point
is that a packaged task produces a reviewable draft whose value the kind's
output contract accepts, and that the prompt the runner received carried the
kind's instructions, its packaged context and its contract.
"""

from __future__ import annotations

import http.server
import json
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from clear_record.service import (
    TASK_KINDS,
    CommandRunner,
    EndpointRunner,
    OutputContractError,
    contract_for,
    glossary_collection_task,
    minutes_task,
    plan_for,
    run_task,
    transcript_check_task,
)

# --- the canned answers a model would return per kind ----------------------- #

_ANSWERS: dict[str, dict] = {
    "glossary_collection": {
        "terms": [
            {
                "term": "Falcon",
                "reading": "FAL-kun",
                "aliases": ["falcon"],
                "definition": "a tracked object",
                "evidence": "00:00:03 [mic] the falcon is up",
            }
        ]
    },
    "transcript_check": {
        "revision": "00:00:03 [mic] the Falcon is up",
        "changes": [{"before": "falcon", "after": "Falcon", "reason": "glossary term"}],
    },
    "minutes": {
        "meeting": "Kickoff",
        "project": "Ops",
        "attendees": ["Ada"],
        "decisions": ["ship it"],
        "actions": ["Ada writes the docs"],
        "body": "# Kickoff\n\nWe shipped it.",
    },
}

_TRANSCRIPT = "00:00:03 [mic] the falcon is up"
_GLOSSARY = "Falcon\n"
_CONTEXT = "project: ops\nmeeting: kickoff"


def _packaged(kind: str):
    """The task a builder packages for ``kind``, with every declared section."""
    if kind == "glossary_collection":
        return glossary_collection_task(
            "ops", "kickoff", transcript=_TRANSCRIPT, glossary=_GLOSSARY
        )
    if kind == "transcript_check":
        return transcript_check_task(
            "ops",
            "kickoff",
            transcript=_TRANSCRIPT,
            glossary=_GLOSSARY,
            context=_CONTEXT,
        )
    return minutes_task(
        "ops", "kickoff", transcript=_TRANSCRIPT, glossary=_GLOSSARY, context=_CONTEXT
    )


# --- a local fake endpoint -------------------------------------------------- #


class _SilentServer(http.server.ThreadingHTTPServer):
    """A server that stays quiet when a client goes away mid-response."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        pass


class _FakeEndpoint:
    """A chat-completions stub that records what it was sent and replies once."""

    def __init__(self, content: str) -> None:
        self.content = content
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
                body = json.dumps(
                    {"choices": [{"message": {"content": endpoint.content}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # noqa: D102
                pass

        self._server = _SilentServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __enter__(self) -> _FakeEndpoint:
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# --- the endpoint path ------------------------------------------------------ #


@pytest.mark.parametrize("kind", TASK_KINDS)
def test_each_kind_runs_to_a_contract_valid_draft(kind: str, tmp_path: Path) -> None:
    answer = json.dumps(_ANSWERS[kind])
    with _FakeEndpoint(answer) as endpoint:
        draft = run_task(
            _packaged(kind),
            tmp_path,
            runner=EndpointRunner(endpoint.url, model="test-model"),
        )

    # Draft first: the run itself never accepts.
    assert draft.review_state == "draft"
    assert not draft.accepted and not draft.rejected
    # The value is exactly what the kind's own contract validates.
    assert draft.value == contract_for(kind).parse(answer)
    # Provenance identifies the kind and its one declared step.
    assert draft.provenance.kind == kind
    assert draft.provenance.steps == (plan_for(kind)[0].name,)
    assert draft.provenance.model == "test-model"

    # The prompt carried the packaged context, the instructions and the contract.
    _headers, sent = endpoint.requests[0]
    prompt = sent["messages"][-1]["content"]
    assert _TRANSCRIPT in prompt
    assert "Falcon" in prompt
    assert plan_for(kind)[0].instructions.splitlines()[0] in prompt
    assert contract_for(kind).describe() in prompt


def test_a_malformed_kind_answer_writes_no_draft(tmp_path: Path) -> None:
    """A kind's contract is the acceptance: a bad answer fails the whole task."""
    with _FakeEndpoint(json.dumps({"meeting": "Kickoff"})) as endpoint:
        with pytest.raises(OutputContractError, match="missing required key"):
            run_task(
                _packaged("minutes"),
                tmp_path,
                runner=EndpointRunner(endpoint.url),
            )
    assert list(tmp_path.rglob("run.json")) == []


# --- the command path ------------------------------------------------------- #

_FAKE_CHECK_AGENT = """\
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
            "saw_transcript": "the falcon is up" in prompt,
            "saw_contract": "Output contract" in prompt,
        }
    ),
    encoding="utf-8",
)
pathlib.Path(args["--out"]).write_text(
    json.dumps(
        {
            "revision": "00:00:03 [mic] the Falcon is up",
            "changes": [
                {"before": "falcon", "after": "Falcon", "reason": "glossary term"}
            ],
        }
    ),
    encoding="utf-8",
)
"""


def test_a_fake_command_drives_a_packaged_kind(tmp_path: Path) -> None:
    script = tmp_path / "fake_check_agent.py"
    script.write_text(textwrap.dedent(_FAKE_CHECK_AGENT), encoding="utf-8")
    template = (
        f"{sys.executable} {script} --input {{input_json}} --out {{output_file}} "
        "--prompt {prompt_file}"
    )

    draft = run_task(
        _packaged("transcript_check"),
        tmp_path / "runs",
        runner=CommandRunner(template),
    )

    assert draft.provenance.runner == "command"
    assert draft.value == _ANSWERS["transcript_check"]
    receipt = json.loads((draft.run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt == {
        "kind": "transcript_check",
        "saw_transcript": True,
        "saw_contract": True,
    }
