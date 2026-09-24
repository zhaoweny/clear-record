"""i18n boundaries, proven with a **transforming pseudo-catalog**.

Nothing here ships a translation. A :class:`gettext.NullTranslations` subclass
wraps every message in guillemets, so the tests can show:

- ``tr`` really is consulted — in a **template** and in **Python**;
- and it is **not** consulted on the machine surfaces: JSONL logs, the JSON API,
  export artifacts and the diagnostics bundle. Those are the user's data or
  machine-read, and a translated status word or log line would break parsing
  (``docs/i18n.md``).
"""

from __future__ import annotations

import dataclasses
import gettext
import json
import re
from contextlib import closing

import click
import pytest
from fastapi.testclient import TestClient

from clear_record.pipeline import stages
from clear_record.cli.cli import _build_group, _split_value
from clear_record.pipeline.workspace import Workspace
from clear_record.core import (
    PipelineOptions,
    RecordDocument,
    i18n,
    log_event,
    log_path,
)
from clear_record.service import Registry, RunManager, managed, setup
from clear_record.service.diagnostics import BundleFacts, build_bundle
from clear_record.web.app import create_app


class _Pseudo(gettext.NullTranslations):
    """Wrap every message; a plural picks the same form English would."""

    def gettext(self, message: str) -> str:  # type: ignore[override]
        return f"«{message}»"

    def ngettext(self, singular: str, plural: str, n: int) -> str:  # type: ignore[override]
        return f"«{singular if n == 1 else plural}»"


@pytest.fixture()
def pseudo() -> gettext.NullTranslations:
    translations = _Pseudo()
    i18n.use(translations)
    return translations


# --- tr is consulted ------------------------------------------------------- #
def test_tr_is_consulted_in_a_template(pseudo, tmp_path) -> None:
    client = TestClient(
        create_app(
            Registry.open(db_path=tmp_path / "r.sqlite3"),
            trusted_hosts=("testserver",),
        )
    )
    assert "«No projects yet.»" in client.get("/ui/projects").text
    # A returning user: the first-run redirect to /setup is not what this test
    # is about, and the marker keeps `/` on the workspace page (ticket 04).
    setup.record_seen_version()
    home = client.get("/")
    assert "«project console»" in home.text
    assert "«Add project»" in home.text


def test_tr_is_consulted_in_python(pseudo) -> None:
    """A CLI runtime error and the group help both go through ``tr``."""
    with pytest.raises(click.UsageError) as excinfo:
        _split_value(True, True)
    assert "«" in str(excinfo.value)

    assert _build_group().help.startswith("«")


# --- the service's user-facing errors are translated ----------------------- #
def _managed_app(tmp_path, monkeypatch):
    """A console app whose managed root is ``tmp_path`` (no ASR, no network)."""
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    client = TestClient(
        create_app(
            Registry.open(db_path=tmp_path / "r.sqlite3"),
            trusted_hosts=("testserver",),
        )
    )
    client.post("/api/projects", json={"name": "Ops"})
    meeting = client.post(
        "/api/projects/ops/meetings", json={"title": "Kickoff", "managed": True}
    ).json()
    return client, meeting


def test_a_guard_reason_is_translated_not_just_its_label(pseudo, tmp_path, monkeypatch):
    """The guard's *reason* — its ID plus parameters — is translated.

    The label was already a ``tr`` string; the point of this test is the reason:
    it is composed by the service as a stable ID + parameters and rendered by
    the console, so the size and the actionable hint come through translated.
    """
    client, meeting = _managed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(managed, "max_upload_bytes", lambda: 4)

    panel = client.post(
        f"/ui/meetings/{meeting['id']}/tapes/upload",
        files={"file": ("a.wav", b"0123456789", "audio/wav")},
    )

    assert panel.status_code == 200
    assert (
        "«the upload is 10 B, over the 4 B limit; raise CR_MAX_UPLOAD_BYTES "
        "to allow it»" in panel.text
    )


def test_the_json_api_refusal_stays_english(pseudo, tmp_path, monkeypatch):
    """The same guard failure is machine-read in the API: it must stay English."""
    client, meeting = _managed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(managed, "max_upload_bytes", lambda: 4)

    res = client.post(
        f"/api/meetings/{meeting['id']}/tapes",
        files={"file": ("a.wav", b"0123456789", "audio/wav")},
    )

    assert res.status_code == 413
    detail = res.json()["detail"]
    assert detail == (
        "the upload is 10 B, over the 4 B limit; raise CR_MAX_UPLOAD_BYTES to allow it"
    )
    assert "«" not in detail


# --- the refused row: a translated frame over a machine-facing detail ------- #

#: The two frames a refused stored-options row is announced with, and a row each
#: one is reached by. The frame is the half that has a catalog entry; what follows
#: it is the fields pydantic refused, which does not (``docs/i18n.md``).
_REFUSED_ROWS = (
    ('{"backend": "apple", "jobs": "many"}', "run {run_id} carries malformed options"),
    ("not json at all", "run {run_id} carries options that are not JSON"),
)


def _store_run_options(registry: Registry, run_id: int, text: str) -> None:
    """Put ``text`` in the run's stored-options column, as another build might have."""
    import sqlite3

    with closing(sqlite3.connect(str(registry.db_path))) as conn, conn:
        conn.execute(
            "UPDATE pipeline_run SET run_options = ? WHERE id = ?", (text, run_id)
        )


def _console_over_a_refused_row(tmp_path, stored: str):
    """A console whose registry holds one queued run whose options are ``stored``.

    The queue is stopped **by construction**: ``start_queue=False`` starts no
    drain thread at all, so nothing can claim the row on a rescan and quarantine
    it (a live queue does exactly that — it is the row's own test, below) and
    no timing decides whether the read sees the row. Stopping a queue that is
    already running would be a schedule, not a state: ``shutdown`` cannot
    un-take a row a rescan already reached.
    """
    registry = Registry.open(db_path=tmp_path / "r.sqlite3")
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    run = registry.create_run(
        meeting.id,
        backend="apple",
        origin="cli",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    _store_run_options(registry, run.id, stored)
    manager = RunManager(registry, start_queue=False)
    client = TestClient(
        create_app(registry, runs=manager, trusted_hosts=("testserver",))
    )
    return client, registry, run.id


@pytest.mark.parametrize(("stored", "frame"), _REFUSED_ROWS)
def test_the_refused_row_page_translates_the_frame_not_the_detail(
    pseudo, tmp_path, stored, frame
) -> None:
    """The 409 page composes one translated frame and one untranslated detail.

    Both halves of ``MalformedRunOptions`` are user-visible here, and they are
    deliberately different: a zh_CN console reads *which run* and *that its options
    cannot be read* in Chinese, while the field list pydantic produced follows it
    exactly as it stands — a field name has no translation, and a half-translated
    sentence would read worse than an English one
    (``docs/i18n.md``: never translated).
    """
    client, _, run_id = _console_over_a_refused_row(tmp_path, stored)

    page = client.get("/")

    assert page.status_code == 409, page.text
    rendered = re.search(r'<p class="muted">(.*?)</p>', page.text, re.S)
    assert rendered is not None, page.text
    translated, _, detail = rendered.group(1).partition(": ")
    assert translated == f"«{frame.format(run_id=run_id)}»"
    assert detail and "«" not in detail


@pytest.mark.parametrize(("stored", "frame"), _REFUSED_ROWS)
def test_the_refused_row_stays_english_on_the_machine_surfaces(
    pseudo, tmp_path, stored, frame
) -> None:
    """The API's ``detail`` is one English sentence: the frame is not translated.

    A script reads the whole refusal through the JSON API, so the frame it gets is
    the English message ID — the same text ``str(exc)`` carries and the quarantine
    writes into the run's ``error`` column below it.
    """
    client, _, run_id = _console_over_a_refused_row(tmp_path, stored)

    res = client.get(f"/api/runs/{run_id}")

    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail.startswith(frame.format(run_id=run_id) + ": ")
    assert "«" not in detail


def test_the_quarantine_keeps_the_detail_english(pseudo, tmp_path) -> None:
    """The run's ``error`` column: the reader's sentence and the row, untranslated.

    ``Registry.fail_unreadable_run`` takes the text out of the column no reader can
    read and puts it in the run's record. The *lead-in* above it is a message ID
    (translated where the row is written), and everything it introduces — the
    reader's own sentence and the row exactly as it stood — stays English, which is
    what ``docs/i18n.md`` declares and what a bug report has to be able to quote.
    """
    stored = '{"backend": "apple", "jobs": "many"}'
    _client, registry, refused_id = _console_over_a_refused_row(tmp_path, stored)
    # A second run, so the drain has work after it quarantines the head of the
    # FIFO — the queue's wait needs a run that can actually finish.
    tape = tmp_path / "later.wav"
    tape.write_bytes(b"RIFFfake")
    later_workspace = tmp_path / "later"
    later_workspace.mkdir()
    later_meeting = registry.create_meeting(
        "ops", "Retro", workspace_path=str(later_workspace)
    )
    registry.set_recording_set(later_meeting.id, [str(tape)])
    later = registry.create_run(
        later_meeting.id,
        backend="apple",
        origin="cli",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )

    manager = RunManager(registry, pipeline=lambda *args: None)
    assert manager.wait(later.id, timeout=10).status == "done"

    quarantined = registry.get_run(refused_id)
    assert quarantined is not None and quarantined.error is not None
    error = quarantined.error
    # The lead-in is a message ID, so it is the translated half — under this
    # pseudo-catalog it arrives wrapped (its own leading newline is inside the
    # message, which is why the quote opens before the line break).
    lead = "the stored options this build cannot read are kept below"
    opened = error.index(f"«\n{lead}")
    # Everything before that quote is machine-facing and untranslated: the
    # reader's sentence, which names the run, and the field detail it carries.
    assert error.startswith(f"run {refused_id} carries malformed options: ")
    assert "«" not in error[:opened]
    # And the row itself is kept exactly as it stood — the reason the column is
    # cleared into the record rather than dropped.
    assert stored in error


def test_the_console_translates_the_pipeline_auto_explanation(pseudo) -> None:
    """``--auto``'s explanation is composed in the pipeline but shown by the console.

    The resolvers keep it English for the terminal and the run meta; the meta
    also records the stable ID + parameters, and the console renders *that* with
    ``tr``, so the explanatory UI text is translated where the user reads it.
    """
    from clear_record.pipeline.auto import AutoProbe, resolve_auto
    from clear_record.web.app import _auto_view

    choice = resolve_auto(
        AutoProbe(
            available_backends=("apple",),
            vram_gb=8.0,
            cpu_count=16,
            models_on_disk=frozenset({"small", "medium"}),
            duration_s=600.0,
            channels=1,
            language=None,
        )
    )
    assert "«" not in choice.explanation  # the terminal/machine form is English
    meta = {
        "auto": {
            "explanation": choice.explanation,
            "message": choice.message.as_json(),
            "chose": ["profile"],
        }
    }

    rendered = _auto_view(meta)["explanations"]

    assert len(rendered) == 1
    assert rendered[0].startswith("«--auto: chose")
    # The nested reason phrases are translated too, not left as English leaves.
    assert "«a short tape on a machine that can afford it»" in rendered[0]


# --- machine surfaces stay English ----------------------------------------- #
def test_log_records_stay_untranslated(pseudo, tmp_path, monkeypatch) -> None:
    """A log line is JSONL for machines; ``tr`` must not touch it."""
    monkeypatch.setenv("CR_LOG_DIR", str(tmp_path / "logs"))
    log_event("info", "web", "ui.ready", message="ready")
    line = log_path().read_text(encoding="utf-8").strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "ui.ready"
    assert record["message"] == "ready"
    assert "«" not in line


def test_json_api_stays_untranslated(pseudo, tmp_path) -> None:
    """Status values in the JSON API are machine-read, not UI text."""
    client = TestClient(
        create_app(
            Registry.open(db_path=tmp_path / "r.sqlite3"),
            trusted_hosts=("testserver",),
        )
    )
    assert client.get("/api/health").json()["status"] == "ok"

    assert client.post("/api/projects", json={"name": "Ops"}).status_code == 201
    term = client.post("/api/projects/ops/glossary", json={"term": "Falcon"}).json()
    assert term["status"] == "candidate"
    assert "«" not in json.dumps(term)


def test_exports_stay_untranslated(pseudo, tmp_path) -> None:
    """The record is the user's data, exported in the language they spoke."""
    workspace = tmp_path / "rec"
    workspace.mkdir()
    Workspace.at(workspace).write_record(
        RecordDocument(sources=(), alignment=None, segments=())
    )
    written = stages.export(str(workspace))
    text = written["md"].read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# Record"
    assert "«" not in text


def test_diagnostics_bundle_stays_untranslated(pseudo) -> None:
    """The bundle is redacted plain text for triage; keep it parseable."""
    bundle = build_bundle(
        BundleFacts(
            version="0.0.0",
            python="3.12",
            platform="test",
            machine="test",
            backends={},
            options={},
        )
    )
    assert bundle.startswith("clear-record diagnostics bundle")
    assert "WITHHELD by default" in bundle
    assert "«" not in bundle


def test_the_backends_command_stays_english_under_a_catalog(
    pseudo, monkeypatch
) -> None:
    """The terminal reads the message's English form, never the catalog's."""
    from click.testing import CliRunner

    import clear_record.providers.backends as provider_backends
    from clear_record.core.i18n import deferred
    from clear_record.core.message import Message
    from clear_record.providers import Availability, BackendBase, BackendInfo

    class _FakeSystemBackend(BackendBase):
        def __init__(self) -> None:
            self.info = BackendInfo(
                id="apple-speech",
                vendor="Apple",
                frameworks=("Speech",),
                description="fake system backend",
                default_model="system",
                runtime="system",
                chunked=False,
            )

        def availability(self) -> Availability:
            return Availability(
                False,
                Message(
                    deferred("requires macOS {major}+ (this is {host})"),
                    (("major", 26), ("host", "Linux")),
                ),
            )

        def transcribe(self, audio_path, **kwargs):  # pragma: no cover - unused
            raise AssertionError

    monkeypatch.setitem(
        provider_backends.BACKENDS, "apple-speech", _FakeSystemBackend()
    )

    result = CliRunner().invoke(_build_group(), ["backends", "--all"])

    assert result.exit_code == 0
    line = next(
        line for line in result.output.splitlines() if line.startswith("apple-speech")
    )
    assert "requires macOS 26+ (this is Linux)" in line
    assert "«" not in line


def test_the_diagnostics_reason_stays_english(pseudo) -> None:
    """The bundle is machine-read: the reason renders in English, untranslated."""
    message_node = {
        "id": "requires macOS {major}+ (this is {host})",
        "params": {"major": 26, "host": "Linux"},
    }
    bundle = build_bundle(
        BundleFacts(
            version="0.0.0",
            python="3.12",
            platform="test",
            machine="test",
            backends={"apple-speech": {"available": False, "reason": message_node}},
            options={},
        )
    )
    assert "requires macOS 26+ (this is Linux)" in bundle
    assert "«" not in bundle


# --- the shipped catalog is translated, not just fresh ---------------------- #

#: The ids the zh_CN catalog deliberately leaves untranslated: the pre-existing
#: per-option --help text the catalog header keeps English ("the terminal is
#: not the interface", owner 2026-09-15).
#:
#: This is an **exact set**, not a count budget. i18n-check guards *freshness*
#: -- source ids vs catalog ids, and the compiled bytes -- never translation: a
#: new tr() string lands with an empty msgstr and silently renders English
#: under zh_CN. An earlier B1 slice shipped exactly that. A count alone would
#: hide a swap (a reworded msgid drops one known id while a new untranslated id
#: takes its place), so every untranslated id must be one of these, and each of
#: these must still be untranslated.
#:
#: An entry counts as untranslated when its msgstr is empty, when **any** plural
#: form is empty, or when it is flagged fuzzy: Babel omits fuzzy entries from
#: the compiled catalog, so at runtime they behave exactly like an empty one.
UNTRANSLATED_IDS = frozenset(
    {
        "bind address (default localhost)",
        "do not open a browser window",
        "keep this process owning the node: a server that stops without being "
        "asked is started again, while a stop request or a signal ends it as it "
        "does an unsupervised node",
        "override the app data directory (default: CR_DATA_DIR / the platform "
        "data directory)",
        "port (default {port})",
        "raise diagnostics log detail (the flag form of CR_LOG_LEVEL=debug); the "
        "log is written to the app state directory, never to stdout",
        "set up Tailscale Serve for this port, trust this machine's tailnet "
        "name, and print its https URL. Serve runs in the foreground alongside "
        "the console and stops with it. The tailnet is the authentication: "
        "anyone on your tailnet can reach the console.",
        "the tailnet HTTPS port Serve exposes (default: the same as --port); "
        "requires --tailscale",
        "trust NAME instead of the machine's resolved tailnet name (requires "
        "--tailscale)",
    }
)

#: The non-[bench] ids this slice added, which the console and the terminal
#: render directly. They stay pinned even if UNTRANSLATED_IDS grows.
BENCH_IDS = (
    "accuracy",
    "coverage",
    "fit",
    "jobs",
    "mean confidence",
    "memory",
    "nothing (chosen by hand)",
    "realtime",
    "similarity",
    "speed",
    "WER",
    "auto chose",
    "the run has no workspace to measure",
    "the workspace has no readable record to score",
    "the run recorded no cost, so this axis is unknown",
    "the run recorded no worker memory",
    "there is no run record, so this axis is unknown",
    "no decoder worker ran: every chunk was reused from the cache",
    "no decoder worker's memory could be sampled during the transcribe stage",
    "peak worker memory is not measurable on this platform",
)


def _po_entries(path) -> list[tuple[str, tuple[str, ...], bool]]:
    """(msgid, msgstrs, fuzzy) for every live entry in a .po file.

    msgstrs holds one value per plural form (a singular entry has exactly one).
    A deliberately small stdlib reader: this suite must not need Babel (it
    lives only in the i18n dependency group, which just verify does not
    install). Obsolete entries are skipped -- they are not compiled and nothing
    renders them.
    """
    entries: list[tuple[str, tuple[str, ...], bool]] = []
    msgid: str | None = None
    forms: list[str] = []
    current = ""
    fuzzy = False
    in_msgstr = False
    target = "id"

    def _unquote(line: str) -> str:
        return json.loads(line[line.index('"') :])

    def _flush_form() -> None:
        nonlocal current
        forms.append(current)
        current = ""

    for line in [*path.read_text(encoding="utf-8").splitlines(), ""]:
        if not line.strip():
            if msgid is not None:
                if in_msgstr:
                    _flush_form()
                entries.append((msgid, tuple(forms), fuzzy))
            msgid, forms, current, fuzzy, in_msgstr, target = (
                None,
                [],
                "",
                False,
                False,
                "id",
            )
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                fuzzy = True
            continue
        if line.startswith("msgid "):
            msgid = _unquote(line)
            target = "id"
        elif line.startswith("msgid_plural "):
            target = "id_plural"  # the plural text is not needed here
        elif line.startswith("msgstr"):
            if in_msgstr:
                _flush_form()
            current = _unquote(line)
            in_msgstr = True
            target = "str"
        elif line.startswith('"') and msgid is not None:
            if target == "str":
                current += _unquote(line)
            elif target == "id":
                msgid += _unquote(line)
    return entries


def test_the_catalogs_untranslated_set_is_exactly_the_documented_one() -> None:
    """The untranslated set is pinned by id, never by a count budget.

    A count can hide a swap: a reworded msgid drops one known-English id while
    a new untranslated id takes its place, and the total is unchanged. So every
    untranslated id must be one of the documented per-option --help entries,
    and each of those must still be there. A plural entry counts as
    untranslated when any of its forms is empty. The bench ids and the bench
    renderer lines are pinned separately as the evidence for this slice.
    """
    po = i18n.LOCALES_DIR / "zh_CN" / "LC_MESSAGES" / "messages.po"
    entries = {
        msgid: (forms, fuzzy) for msgid, forms, fuzzy in _po_entries(po) if msgid
    }

    untranslated = {
        msgid
        for msgid, (forms, fuzzy) in entries.items()
        if fuzzy or not forms or any(not form for form in forms)
    }
    difference = sorted(untranslated ^ set(UNTRANSLATED_IDS))
    assert untranslated == set(UNTRANSLATED_IDS), (
        f"the zh_CN untranslated set changed ({difference}): translate the new "
        "id, or -- if one of the deliberately English per-option --help ids was "
        "translated or reworded -- update UNTRANSLATED_IDS"
    )

    missing = [
        msgid
        for msgid in BENCH_IDS
        if msgid not in entries
        or entries[msgid][1]
        or not entries[msgid][0]
        or any(not form for form in entries[msgid][0])
    ]
    assert missing == [], f"the bench ids lost their translation: {missing}"

    terminal = [
        msgid
        for msgid, (forms, fuzzy) in entries.items()
        if msgid.startswith("[bench] ")
        and (fuzzy or not forms or any(not form for form in forms))
    ]
    assert terminal == [], f"the bench renderer lost its translation: {terminal}"
