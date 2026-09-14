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
from clear_record.service import Registry, managed
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
