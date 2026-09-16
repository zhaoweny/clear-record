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

import gettext
import json

import click
import pytest
from fastapi.testclient import TestClient

from clear_record.cli import stages
from clear_record.cli.cli import _build_group, _split_value
from clear_record.cli.workspace import Workspace
from clear_record.core import RecordDocument, i18n, log_event, log_path
from clear_record.service import Registry, managed, setup
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


def test_the_console_translates_the_cli_auto_explanation(pseudo) -> None:
    """``--auto``'s explanation is composed in the CLI but shown by the console.

    The resolvers keep it English for the terminal and the run meta; the meta
    also records the stable ID + parameters, and the console renders *that* with
    ``tr``, so the explanatory UI text is translated where the user reads it.
    """
    from clear_record.cli.auto import AutoProbe, resolve_auto
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
    written = stages.export(str(workspace), formats=["md"])
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

#: How many entries the zh_CN catalog may leave untranslated.
#:
#: ``just i18n-check`` guards *freshness* -- source ids vs catalog ids, and the
#: compiled bytes -- never translation: a new ``tr()`` string lands with an
#: empty ``msgstr`` and silently renders English under zh_CN. An earlier B1
#: slice shipped exactly that, so it is a failing test here.
#:
#: An entry counts as untranslated when its ``msgstr`` is empty **or** it is
#: flagged ``#, fuzzy``: Babel omits fuzzy entries from the compiled catalog,
#: so at runtime they behave exactly like an empty one. The baseline is the
#: eight pre-existing per-option ``--help`` entries the catalog header
#: deliberately leaves English ("the terminal is not the interface", owner
#: 2026-09-15); translate new ids rather than raising this number.
UNTRANSLATED_BASELINE = 8

#: The non-``[bench]`` ids this slice added, which the console and the terminal
#: render directly. They stay pinned even if the baseline above is ever raised.
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
    "there is no run record to explain",
    "no decoder worker ran: every chunk was reused from the cache",
    "no decoder worker's memory could be sampled during the transcribe stage",
    "peak worker memory is not measurable on this platform",
)


def _po_entries(path) -> list[tuple[str, str, bool]]:
    """``(msgid, msgstr, fuzzy)`` for every live entry in a ``.po`` file.

    A deliberately small stdlib reader: this suite must not need Babel (it
    lives only in the ``i18n`` dependency group, which ``just verify`` does not
    install). Obsolete entries (``#~``) are skipped -- they are not compiled
    and nothing renders them.
    """
    entries: list[tuple[str, str, bool]] = []
    msgid: str | None = None
    msgstr = ""
    fuzzy = False
    in_msgstr = False

    def _unquote(line: str) -> str:
        return json.loads(line[line.index('"') :])

    for line in [*path.read_text(encoding="utf-8").splitlines(), ""]:
        if not line.strip():
            if msgid is not None:
                entries.append((msgid, msgstr, fuzzy))
            msgid, msgstr, fuzzy, in_msgstr = None, "", False, False
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                fuzzy = True
            continue
        if line.startswith("msgid "):
            msgid = _unquote(line)
        elif line.startswith("msgstr"):
            msgstr = _unquote(line)
            in_msgstr = True
        elif line.startswith('"') and msgid is not None:
            if in_msgstr:
                msgstr += _unquote(line)
            else:
                msgid += _unquote(line)
    return entries


def test_every_new_string_is_translated_in_the_shipped_catalog() -> None:
    """A message that renders English under zh_CN fails the gate.

    The count is the guard; the explicit families are the evidence for exactly
    the strings this slice introduced.
    """
    po = i18n.LOCALES_DIR / "zh_CN" / "LC_MESSAGES" / "messages.po"
    entries = {
        msgid: (msgstr, fuzzy) for msgid, msgstr, fuzzy in _po_entries(po) if msgid
    }

    untranslated = sorted(
        msgid for msgid, (msgstr, fuzzy) in entries.items() if not msgstr or fuzzy
    )
    assert len(untranslated) <= UNTRANSLATED_BASELINE, (
        f"zh_CN has {len(untranslated)} untranslated entries, over the "
        f"{UNTRANSLATED_BASELINE} baseline; translate the new ids: "
        f"{untranslated[:5]}"
    )

    missing = [
        msgid
        for msgid in BENCH_IDS
        if msgid not in entries or not entries[msgid][0] or entries[msgid][1]
    ]
    assert missing == [], f"the bench ids lost their translation: {missing}"

    terminal = [
        msgid
        for msgid, (msgstr, fuzzy) in entries.items()
        if msgid.startswith("[bench] ") and (not msgstr or fuzzy)
    ]
    assert terminal == [], f"the bench renderer lost its translation: {terminal}"
