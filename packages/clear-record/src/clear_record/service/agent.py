"""The provider-agnostic agent-task runner seam (ADR-0018).

One seam, and two runners behind it:

::

    draft = run_task(task, out_dir, runner=...)

where ``task`` is a :class:`~clear_record.service.agent_tasks.AgentTask`
(kind + inputs + instructions), and the result is a **draft** artifact plus
:class:`Provenance` — the runner kind, the model, the prompt hash, the context
hash and the timestamps. **Nothing here names an agent runtime, a vendor SDK or
a bundled harness**, so the seam's future is additive: a bundled harness is a
fourth :class:`Runner`, not a rewrite. ``EndpointRunner`` (the default) speaks
the OpenAI-compatible ``/chat/completions`` protocol of any server the user
brings; ``CommandRunner`` (the advanced path) runs the user's own command
template.

Properties the design holds to:

- **BYOK, and the key is never stored.** ``api_key_env`` names an environment
  variable; the value is read at call time, sent as a bearer token, and never
  written to the config, the provenance or the artifact. A local server needs no
  key at all — with no ``api_key_env`` no ``Authorization`` header is sent. An
  env that *is* named but unset fails closed rather than sending unauthenticated.
- **Stdlib only.** The endpoint call uses :mod:`urllib`, so the base install
  gains no HTTP dependency (ADR-0018's ``[DESIGN]``).
- **A contract or nothing.** Every step's response must parse into its task
  kind's :class:`~clear_record.service.agent_tasks.OutputContract`; a malformed
  response raises :class:`OutputContractError` and writes no draft, so a partial
  answer is never accepted.
- **A fixed pipeline, not a loop.** The seam runs exactly the steps
  :func:`~clear_record.service.agent_tasks.plan_for` declares, chaining each
  step's raw output into the next.
- **Draft first.** Output lands as a draft; nothing is promoted until an
  explicit :func:`accept_draft` (or :func:`reject_draft`). Accepting is what
  promotes it.

A run's durable footprint is one directory ``<out_dir>/<kind>-<run_id>/``::

    output.json   the validated artifact (canonical JSON)
    run.json      the provenance plus the review state
    input.json    the packaged task context the run was based on
    prompt.txt    the last prompt sent (written by CommandRunner)

so a run can be inspected, re-read (:func:`read_draft`) and reviewed with no
database and no network.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import shlex
import string
import subprocess
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from clear_record.service.agent_tasks import (
    TASK_KINDS,
    AgentTask,
    AgentTaskError,
    OutputContract,
    OutputContractError,
    TaskStep,
    canonical_payload,
    context_hash,
    plan_for,
    prompt_hash,
    render_prompt,
)
from clear_record.service.paths import config_path

# --- errors ----------------------------------------------------------------- #


class RunnerError(AgentTaskError):
    """A runner could not produce output (transport, exit status, no output)."""


class AgentConfigError(AgentTaskError):
    """No usable agent is configured, or a command template is malformed."""


#: The placeholder names a command template may use. ``{prompt_file}`` and
#: ``{input_json}`` are written for the command; ``{output_file}`` is where the
#: command must write its answer; ``{project}`` and ``{meeting}`` are plain text.
COMMAND_PLACEHOLDERS: tuple[str, ...] = (
    "prompt_file",
    "input_json",
    "output_file",
    "project",
    "meeting",
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


# --- the runner interface --------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class RunnerRequest:
    """One call: a step, its prompt, the packaged context and where to write.

    ``input_json`` is the packaged task context (the same object whose hash is
    the run's context hash), ``output_file`` is where a command runner must
    write its answer, and the model is the runner's own (an endpoint runner's
    configured model). An endpoint runner ignores ``output_file`` and returns
    its text; a command runner writes the file and the seam reads it back.
    """

    step: str
    prompt: str
    input_json: Mapping
    project: str
    meeting: str
    kind: str
    output_file: Path


@dataclasses.dataclass(frozen=True)
class RunnerOutput:
    """What one runner call produced: the response text and the model label."""

    text: str
    model: str | None = None


class Runner:
    """The seam's execution interface — the only thing a runtime must satisfy.

    A runner takes a :class:`RunnerRequest` and returns a
    :class:`RunnerOutput`, or raises :class:`RunnerError`. It never sees the
    registry, the config's key (only an environment variable name), or another
    runner. ``kind`` names the runner *kind* ("endpoint", "command", a future
    "mcp") for provenance; it is not a runtime or a vendor.
    """

    kind: str = ""

    def run(self, request: RunnerRequest) -> RunnerOutput:  # pragma: no cover
        raise NotImplementedError


class EndpointRunner(Runner):
    """The default runner: an OpenAI-compatible ``/chat/completions`` server.

    The user brings the endpoint — a local server needs no key; a hosted one is
    BYOK. The credential is a **name**: ``api_key_env`` reads the value from the
    environment at call time and nothing persists it. With no ``api_key_env`` no
    auth header is sent; with one that is unset the call fails closed.
    """

    kind = "endpoint"

    def __init__(
        self,
        endpoint: str,
        *,
        model: str | None = None,
        api_key_env: str | None = None,
        timeout: float = 120.0,
        system_prompt: str | None = None,
        opener: Callable[..., object] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        endpoint = endpoint.strip()
        if not endpoint:
            raise AgentConfigError("endpoint must not be blank")
        self.endpoint = endpoint
        self.model = model or None
        self.api_key_env = api_key_env or None
        self.timeout = timeout
        self.system_prompt = system_prompt
        self._opener = opener or urllib.request.urlopen
        self._environ = environ

    @property
    def completion_url(self) -> str:
        """The full ``/chat/completions`` URL, composed once from the base."""
        base = self.endpoint.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _api_key(self) -> str | None:
        """The bearer token from the environment, or a fail-closed error."""
        if self.api_key_env is None:
            return None  # a local endpoint needs no key
        environ = os.environ if self._environ is None else self._environ
        key = environ.get(self.api_key_env)
        if not key:
            raise RunnerError(
                f"endpoint names api_key_env {self.api_key_env!r}, which is not "
                "set: refusing to call unauthenticated"
            )
        return key

    def run(self, request: RunnerRequest) -> RunnerOutput:
        key = self._api_key()
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": request.prompt})
        payload: dict = {"messages": messages, "temperature": 0}
        if self.model:
            payload["model"] = self.model
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "clear-record-agent/1",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = self.completion_url
        http_request = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        try:
            response = self._opener(http_request, timeout=self.timeout)
            try:
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
        except urllib.error.HTTPError as exc:
            snippet = _body_snippet(exc.read())
            exc.close()
            raise RunnerError(
                f"endpoint returned HTTP {exc.code} from {url}: {snippet}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - any transport failure is one error
            raise RunnerError(
                f"endpoint request to {url} failed: {type(exc).__name__}: {exc}"
            ) from exc

        text = _completion_text(raw.decode("utf-8", errors="replace"), url)
        return RunnerOutput(text=text, model=self.model)


def _body_snippet(body: bytes, limit: int = 300) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    return text[:limit] if text else "(empty body)"


def _completion_text(body: str, url: str) -> str:
    """The assistant text from a chat-completions response, or a useful error."""
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RunnerError(
            f"endpoint {url} returned a non-JSON body ({exc.msg}): {body.strip()[:200]}"
        ) from exc
    try:
        content = document["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RunnerError(
            f"endpoint {url} returned no choices[0].message.content: "
            f"{body.strip()[:200]}"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise RunnerError(f"endpoint {url} returned an empty completion")
    return content


class CommandRunner(Runner):
    """The advanced runner: the user's command template, run as a subprocess.

    The template names the placeholders in :data:`COMMAND_PLACEHOLDERS`; the
    template is formatted, split with :func:`shlex.split` and executed **without
    a shell**, so a placeholder value can never be reinterpreted as syntax. The
    command must exit ``0`` and write its answer to ``{output_file}``; anything
    else is a :class:`RunnerError`.
    """

    kind = "command"

    def __init__(
        self,
        template: str,
        *,
        label: str | None = None,
        timeout: float = 600.0,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        template = template.strip()
        if not template:
            raise AgentConfigError("command template must not be blank")
        unknown = _unknown_placeholders(template)
        if unknown:
            raise AgentConfigError(
                f"command template has unknown placeholder(s) "
                f"{', '.join(sorted(unknown))}; allowed: "
                f"{', '.join(COMMAND_PLACEHOLDERS)}"
            )
        self.template = template
        self.model = label or None
        self.timeout = timeout
        self._runner = runner or subprocess.run
        self._environ = environ

    def run(self, request: RunnerRequest) -> RunnerOutput:
        directory = request.output_file.parent
        directory.mkdir(parents=True, exist_ok=True)
        prompt_file = directory / "prompt.txt"
        input_json = directory / "input.json"
        prompt_file.write_text(request.prompt, encoding="utf-8")
        input_json.write_text(
            json.dumps(request.input_json, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        command = self.template.format(
            prompt_file=prompt_file,
            input_json=input_json,
            output_file=request.output_file,
            project=request.project,
            meeting=request.meeting,
        )
        argv = shlex.split(command)
        if not argv:
            raise RunnerError("command template rendered to an empty command")
        environ = None if self._environ is None else dict(self._environ)
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(directory),
                env=environ,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(
                f"command timed out after {self.timeout}s: {argv[0]}"
            ) from exc
        except OSError as exc:
            raise RunnerError(f"command could not start: {type(exc).__name__}: {exc}")
        if completed.returncode != 0:
            tail = (completed.stderr or "").strip()[-500:]
            raise RunnerError(
                f"command exited {completed.returncode}: {argv[0]}"
                + (f"\n{tail}" if tail else "")
            )
        if not request.output_file.is_file():
            raise RunnerError(
                f"command wrote no output file at {request.output_file}; the "
                "template must write its answer to {output_file}"
            )
        text = request.output_file.read_text(encoding="utf-8")
        if not text.strip():
            raise RunnerError(
                f"command wrote an empty output file at {request.output_file}"
            )
        return RunnerOutput(text=text, model=self.model)


def _unknown_placeholders(template: str) -> set[str]:
    """Template fields outside :data:`COMMAND_PLACEHOLDERS` (a malformed template)."""
    unknown: set[str] = set()
    for _literal, field_name, _spec, _conversion in string.Formatter().parse(template):
        if field_name is None:
            continue
        if field_name not in COMMAND_PLACEHOLDERS:
            unknown.add(field_name)
    return unknown


# --- provenance and the draft ----------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Provenance:
    """What produced an artifact: runner, model, hashes and timestamps.

    ``run_id`` is derived from the kind, the context and prompt hashes and the
    start time, so a run identifies itself from its provenance alone. ``steps``
    is the fixed pipeline that ran; ``runner`` is a runner *kind*, never a
    runtime.
    """

    run_id: str
    kind: str
    project: str
    meeting: str
    runner: str
    model: str | None
    prompt_hash: str
    context_hash: str
    steps: tuple[str, ...]
    started_at: str
    ended_at: str


@dataclasses.dataclass(frozen=True)
class Draft:
    """A produced artifact plus proof of how it was produced, awaiting review.

    ``value`` is the validated, JSON-shaped artifact; ``text`` is its canonical
    serialization (what is on disk at :attr:`artifact_path`).
    :attr:`review_state` is ``"draft"`` until an explicit accept or reject.
    """

    run_dir: Path
    artifact_path: Path
    provenance_path: Path
    kind: str
    project: str
    meeting: str
    text: str
    value: object
    provenance: Provenance
    review_state: str = "draft"

    @property
    def accepted(self) -> bool:
        return self.review_state == "accepted"

    @property
    def rejected(self) -> bool:
        return self.review_state == "rejected"


def _run_id(kind: str, context_digest: str, prompt_digest: str, started_at: str) -> str:
    seed = "|".join([kind, context_digest, prompt_digest, started_at])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def run_task(
    task: AgentTask,
    out_dir: str | Path,
    *,
    runner: Runner | None = None,
    config: AgentConfig | None = None,
    plan: tuple[TaskStep, ...] | None = None,
    clock: Callable[[], str] = _now,
    environ: Mapping[str, str] | None = None,
) -> Draft:
    """Run ``task`` and return its draft artifact plus provenance.

    ``runner`` may be supplied directly (tests, the console's own wiring); when
    omitted it is built from ``config`` (or the resolved process config). Each
    step's response is validated against its contract before the next step runs,
    and the final validated value is written as the draft artifact. A malformed
    response raises :class:`OutputContractError` with no draft written.
    """
    if runner is None:
        resolved = config if config is not None else load_agent_config(environ=environ)
        runner = resolved.runner_for(task.kind)

    steps = plan if plan is not None else plan_for(task.kind)
    if not steps:
        raise AgentTaskError(f"{task.kind}: the task declares no pipeline steps")
    payload = canonical_payload(task)
    context_digest = context_hash(task)
    prompt_digest = prompt_hash(steps)
    started_at = clock()
    run_id = _run_id(task.kind, context_digest, prompt_digest, started_at)
    run_dir = Path(out_dir) / f"{task.kind}-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    value: object = None
    previous: str | None = None
    output = RunnerOutput(text="")
    for step in steps:
        contract = step.resolved_contract(task.kind)
        prompt = render_prompt(step, payload, contract, previous=previous)
        request = RunnerRequest(
            step=step.name,
            prompt=prompt,
            input_json=payload,
            project=task.project,
            meeting=task.meeting,
            kind=task.kind,
            output_file=run_dir / "output.json",
        )
        output = runner.run(request)
        value = contract.parse(output.text)
        previous = output.text
    ended_at = clock()

    provenance = Provenance(
        run_id=run_id,
        kind=task.kind,
        project=task.project,
        meeting=task.meeting,
        runner=runner.kind,
        model=output.model,
        prompt_hash=prompt_digest,
        context_hash=context_digest,
        steps=tuple(step.name for step in steps),
        started_at=started_at,
        ended_at=ended_at,
    )
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    artifact_path = run_dir / "output.json"
    artifact_path.write_text(text, encoding="utf-8")
    return _write_review_state(
        run_dir=run_dir,
        artifact_path=artifact_path,
        kind=task.kind,
        project=task.project,
        meeting=task.meeting,
        text=text,
        value=value,
        provenance=provenance,
        review_state="draft",
    )


def _write_review_state(
    *,
    run_dir: Path,
    artifact_path: Path,
    kind: str,
    project: str,
    meeting: str,
    text: str,
    value: object,
    provenance: Provenance,
    review_state: str,
) -> Draft:
    provenance_path = run_dir / "run.json"
    provenance_path.write_text(
        json.dumps(
            {
                "provenance": dataclasses.asdict(provenance),
                "review_state": review_state,
                "artifact": artifact_path.name,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return Draft(
        run_dir=run_dir,
        artifact_path=artifact_path,
        provenance_path=provenance_path,
        kind=kind,
        project=project,
        meeting=meeting,
        text=text,
        value=value,
        provenance=provenance,
        review_state=review_state,
    )


def accept_draft(draft: Draft) -> Draft:
    """Promote a draft: the explicit act that makes agent output canonical."""
    return _transition(draft, "accepted")


def reject_draft(draft: Draft) -> Draft:
    """Reject a draft, keeping it on disk (and its provenance) as history."""
    return _transition(draft, "rejected")


#: The explicit review states a draft may hold.
REVIEW_STATES: tuple[str, ...] = ("draft", "accepted", "rejected")


def _transition(draft: Draft, state: str) -> Draft:
    if state not in REVIEW_STATES:
        raise AgentTaskError(f"unknown review state {state!r}")
    document = json.loads(draft.provenance_path.read_text(encoding="utf-8"))
    document["review_state"] = state
    draft.provenance_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return dataclasses.replace(draft, review_state=state)


def read_draft(run_dir: str | Path) -> Draft:
    """Reload a run's draft from disk (the review state included).

    Unknown provenance fields are ignored, so a run written by a newer build
    still opens in an older one.
    """
    run_dir = Path(run_dir)
    document = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    known = {field.name for field in dataclasses.fields(Provenance)}
    values = {
        key: value
        for key, value in document.get("provenance", {}).items()
        if key in known
    }
    if isinstance(values.get("steps"), list):
        values["steps"] = tuple(values["steps"])
    provenance = Provenance(**values)
    artifact_path = run_dir / document.get("artifact", "output.json")
    text = artifact_path.read_text(encoding="utf-8")
    return Draft(
        run_dir=run_dir,
        artifact_path=artifact_path,
        provenance_path=run_dir / "run.json",
        kind=provenance.kind,
        project=provenance.project,
        meeting=provenance.meeting,
        text=text,
        value=json.loads(text),
        provenance=provenance,
        review_state=document.get("review_state", "draft"),
    )


# --- configuration (ADR-0007 precedence) ------------------------------------ #

#: ``CR_AGENT_ENDPOINT`` — an OpenAI-compatible base URL (``/chat/completions``
#: appended if absent). The one thing a user must set for the default path.
ENV_ENDPOINT = "CR_AGENT_ENDPOINT"
#: ``CR_AGENT_MODEL`` — the model the endpoint should run.
ENV_MODEL = "CR_AGENT_MODEL"
#: ``CR_AGENT_API_KEY_ENV`` — the *name* of the environment variable holding a
#: hosted endpoint's key. Never the key itself.
ENV_API_KEY_ENV = "CR_AGENT_API_KEY_ENV"
#: ``CR_AGENT_TIMEOUT`` — seconds to wait for a response.
ENV_TIMEOUT = "CR_AGENT_TIMEOUT"

#: The TOML schema, documented where the user sets it::
#:
#:     [agent]
#:     endpoint = "http://127.0.0.1:8080/v1"   # a local server needs no key
#:     model = "a-model-the-server-serves"
#:     api_key_env = "OPENAI_API_KEY"          # a NAME; never the value
#:     timeout = 120
#:
#:     [agent.commands]
#:     glossary_collection = "my-agent --prompt {prompt_file} --in {input_json} --out {output_file}"
#:     transcript_check = "my-agent check --in {input_json} --out {output_file}"
#:     minutes = "my-agent minutes --in {input_json} --out {output_file}"


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    """The resolved agent plumbing: where to point, and how to authenticate.

    There is deliberately **no key field**: ``api_key_env`` is a variable name,
    so the resolved config can be logged, stored or marshalled without carrying
    a secret. ``commands`` maps a task kind to a command template (the advanced
    path); a kind with a template uses :class:`CommandRunner`, otherwise the
    endpoint is used.
    """

    endpoint: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    timeout: float = 120.0
    commands: Mapping[str, str] = dataclasses.field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def runner_for(self, kind: str) -> Runner:
        """The runner this config implies for ``kind``.

        A configured command template wins (it is the advanced, explicit path);
        otherwise the endpoint is the default. Neither configured is an
        actionable error rather than a silent no-op.
        """
        template = self.commands.get(kind)
        if template:
            return CommandRunner(template, label=self.model, timeout=self.timeout)
        if self.endpoint:
            return EndpointRunner(
                self.endpoint,
                model=self.model,
                api_key_env=self.api_key_env,
                timeout=self.timeout,
            )
        raise AgentConfigError(
            f"no agent configured for {kind!r}: set {ENV_ENDPOINT} to an "
            "OpenAI-compatible endpoint (a local server needs no key), or set "
            f"[agent.commands] {kind} to a command template"
        )


def _warn(problem: str) -> None:
    """Surface a config problem on stderr, as one line — never a traceback."""
    print(f"clear-record: agent config: {problem}", file=sys.stderr)


def _agent_table(config_file: str | Path | None) -> tuple[dict, tuple[str, ...]]:
    """The ``[agent]`` table of the config file, or ``({}, problems)``."""
    path = Path(config_file) if config_file is not None else config_path()
    if not path.is_file():
        return {}, ()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, (f"{path}: not valid TOML ({exc})",)
    agent = data.get("agent")
    if agent is None:
        return {}, ()
    if not isinstance(agent, dict):
        return {}, (f"{path}: [agent] must be a table",)
    return agent, ()


def _commands(raw: object, source: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse and validate an ``[agent.commands]`` table."""
    if raw is None:
        return {}, ()
    if not isinstance(raw, dict):
        return {}, (f"{source}: agent.commands must be a table",)
    commands: dict[str, str] = {}
    problems: list[str] = []
    for kind, template in raw.items():
        if kind not in TASK_KINDS:
            problems.append(
                f"{source}: unknown task kind {kind!r} in agent.commands; "
                f"known kinds: {', '.join(TASK_KINDS)}"
            )
            continue
        if not isinstance(template, str) or not template.strip():
            problems.append(f"{source}: agent.commands.{kind} must be a string")
            continue
        unknown = _unknown_placeholders(template)
        if unknown:
            problems.append(
                f"{source}: agent.commands.{kind} has unknown placeholder(s) "
                f"{', '.join(sorted(unknown))}"
            )
            continue
        commands[kind] = template.strip()
    return commands, tuple(problems)


def load_agent_config(
    *,
    environ: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
) -> AgentConfig:
    """Resolve the agent plumbing; never raises and never reads a key's value.

    Precedence (ADR-0007): the ``CR_AGENT_*`` variable > the ``[agent]`` table >
    nothing. Problems (malformed TOML, an unknown placeholder, a bad timeout)
    are surfaced on stderr and returned in :attr:`AgentConfig.problems`, so a
    misconfiguration reads as "why is my agent not running?" rather than a
    traceback. The returned config holds only the env **name**; the secret is
    read later, at call time.
    """
    env = os.environ if environ is None else environ
    table, problems = _agent_table(config_file)
    problems = list(problems)

    commands, command_problems = _commands(
        table.get("commands"), source=str(config_file or config_path())
    )
    problems.extend(command_problems)

    def pick(env_name: str, key: str) -> str | None:
        value = env.get(env_name)
        if value is None or not value.strip():
            raw = table.get(key)
            return str(raw).strip() if raw is not None else None
        return value.strip()

    endpoint = pick(ENV_ENDPOINT, "endpoint")
    model = pick(ENV_MODEL, "model")
    api_key_env = pick(ENV_API_KEY_ENV, "api_key_env")

    timeout = 120.0
    raw_timeout = env.get(ENV_TIMEOUT) or table.get("timeout")
    if raw_timeout is not None:
        try:
            timeout = float(raw_timeout)
            if timeout <= 0:
                raise ValueError("must be positive")
        except (TypeError, ValueError):
            problems.append(
                f"agent timeout {raw_timeout!r} is not a positive number; using 120"
            )
            timeout = 120.0

    for problem in problems:
        _warn(problem)
    return AgentConfig(
        endpoint=endpoint,
        model=model,
        api_key_env=api_key_env,
        timeout=timeout,
        commands=commands,
        problems=tuple(problems),
    )


# --- process-wide default --------------------------------------------------- #

_DEFAULT_LOCK = threading.Lock()
_DEFAULT: AgentConfig | None = None


def default_config() -> AgentConfig:
    """The shared, config-driven agent plumbing (resolved once per process).

    A malformed config never raises: it is surfaced as a stderr warning and
    recorded on :attr:`AgentConfig.problems`.
    """
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                try:
                    _DEFAULT = load_agent_config()
                except Exception as exc:  # noqa: BLE001 - never take the app down
                    problem = f"agent disabled: {type(exc).__name__}: {exc}"
                    _warn(problem)
                    _DEFAULT = AgentConfig(problems=(problem,))
    return _DEFAULT


def reset_default_config() -> None:
    """Drop the cached default config (tests that reconfigure the process)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


__all__ = [
    "AgentConfig",
    "AgentConfigError",
    "COMMAND_PLACEHOLDERS",
    "CommandRunner",
    "Draft",
    "ENV_API_KEY_ENV",
    "ENV_ENDPOINT",
    "ENV_MODEL",
    "ENV_TIMEOUT",
    "EndpointRunner",
    "OutputContract",
    "OutputContractError",
    "Provenance",
    "REVIEW_STATES",
    "Runner",
    "RunnerError",
    "RunnerOutput",
    "RunnerRequest",
    "accept_draft",
    "default_config",
    "load_agent_config",
    "read_draft",
    "reject_draft",
    "reset_default_config",
    "run_task",
]
