"""A stored run's options: the declared row, and what the reader refuses.

``pipeline_run.run_options`` is JSON text, and a row written by an **earlier
release of this application** is the input this seam exists for: the release that
enqueued a run wrote its own ``PipelineOptions``, so a row outlives the build
that wrote it. The tests drive the registry read directly, because that is where
a stored row crosses into a run; rows are written straight into the column, which
is how a registry can hold one at all — the write path refuses a partial row
itself.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import warnings
from typing import get_type_hints

import pytest

import clear_record.service.run_options as run_options
from clear_record.core import (
    RESOLVABLE_FIELDS,
    SUPERSEDED_KEYS,
    PipelineOptions,
)
from clear_record.service import MalformedRunOptions, Registry
from clear_record.service.run_options import SupersededRunOptions

#: A row the released line (``public/releases/v0.2.x``, ``public/main``) wrote:
#: ``dataclasses.asdict`` of *its* :class:`PipelineOptions`, copied from that
#: build's dataclass — which carried ``formats``, between ``reference`` and
#: ``attribute_energy``, and no field this build has lost. Rebuilt here as JSON
#: text, and pinned to today's declaration by
#: :func:`test_the_released_row_is_todays_fields_plus_the_superseded_keys`, so the
#: fixture cannot quietly stop being a released row.
_RELEASED_ROW = {
    "backend": "apple",
    "model": "small",
    "language": "en",
    "model_dir": None,
    "audio_files": ["/tapes/a.wav", "/tapes/b.wav"],
    "split": "auto",
    "glossary": None,
    "chunk_seconds": 30.0,
    "overlap_seconds": 5.0,
    "resume": True,
    "do_diarize": None,
    "speakers": None,
    "reference": None,
    "formats": ["md", "srt"],
    "attribute_energy": False,
    "mixed_source": None,
    "window_s": None,
    "jobs": 1,
    "check_plugin": False,
    "rerun_sources": None,
    "rerun_range": None,
    "profile": "custom",
    "beam_size": None,
    "best_of": None,
    "temperature": None,
    "entropy_thold": None,
    "no_speech_thold": None,
    "max_context": None,
    "threads": None,
}


def _seeded_run(tmp_path) -> tuple[Registry, int]:
    """A registry holding one queued run, with its complete options row."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    run = registry.create_run(
        meeting.id,
        backend="apple",
        origin="cli",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    return registry, run.id


def _store(registry: Registry, run_id: int, row: dict) -> None:
    """Write the column directly — an earlier release's row, or a hand edit."""
    with sqlite3.connect(str(registry.db_path)) as conn:
        conn.execute(
            "UPDATE pipeline_run SET run_options = ? WHERE id = ?",
            (json.dumps(row), run_id),
        )


def _written_row(registry: Registry, run_id: int) -> dict:
    """The row a run stores today, ready to be corrupted."""
    stored = registry.get_run(run_id)
    assert stored is not None and stored.run_options is not None
    return dict(stored.run_options)


def test_a_stored_row_reads_back_as_the_options_it_was(tmp_path) -> None:
    """The whole row survives the JSON column and the model, nothing dropped."""
    registry, run_id = _seeded_run(tmp_path)

    assert registry.get_run(run_id).run_options == dataclasses.asdict(
        PipelineOptions(backend="apple")
    )


def test_the_released_row_is_todays_fields_plus_the_superseded_keys() -> None:
    """The released row is what today's declaration has, plus what it has lost.

    The fixture is a copy of another build's dataclass, so nothing keeps it in
    step with this one; this does. A field added here, or a superseded key
    declared, shows up as a difference — the reader's tolerance is exactly the
    keys in :data:`SUPERSEDED_KEYS`, no more and no less.
    """
    assert set(_RELEASED_ROW) == {
        field.name for field in dataclasses.fields(PipelineOptions)
    } | {row.stored for row in SUPERSEDED_KEYS}
    assert {row.stored for row in SUPERSEDED_KEYS} <= set(_RELEASED_ROW)


def test_a_row_an_earlier_release_wrote_still_reads(tmp_path) -> None:
    """The released line's ``formats`` is settled, not refused (R1/B1).

    Before this, the row refused: it carried a key no current field has, and the
    registry that held it read as malformed for every caller — so an upgrade
    broke the runs the release had queued.
    """
    registry, run_id = _seeded_run(tmp_path)
    _store(registry, run_id, _RELEASED_ROW)

    with pytest.warns(SupersededRunOptions) as warned:
        read = registry.get_run(run_id).run_options

    message = str(warned[0].message)
    assert f"run {run_id}" in message
    assert "formats" in message
    assert read is not None
    assert "formats" not in read
    # Everything the release wrote that this build still has is kept, exactly.
    assert {key: value for key, value in _RELEASED_ROW.items() if key != "formats"} == {
        **read,
        "audio_files": list(read["audio_files"]),
    }


def test_a_row_an_earlier_release_wrote_still_runs(tmp_path) -> None:
    """The read is a read, not a repair the queue cannot use: the options rebuild."""
    registry, run_id = _seeded_run(tmp_path)
    _store(registry, run_id, _RELEASED_ROW)

    with pytest.warns(SupersededRunOptions):
        stored = registry.get_run(run_id)

    options = run_options.RunOptionsRow.model_validate(stored.run_options or {})
    assert options.audio_files == ("/tapes/a.wav", "/tapes/b.wav")
    assert options.chunk_seconds == 30.0


def test_a_superseded_key_with_a_successor_moves_the_value_across(
    tmp_path, monkeypatch
) -> None:
    """A renamed key hands its value to its successor rather than dropping it.

    No key in :data:`SUPERSEDED_KEYS` today is a rename — ``formats`` was removed
    outright — so the mapping is probed by declaring one: the value a released row
    holds lands on the field it now means, and the row does not warn about it.
    """
    registry, run_id = _seeded_run(tmp_path)
    row = _written_row(registry, run_id)
    row.pop("jobs")
    row["workers"] = 3
    _store(registry, run_id, row)

    class Renamed:
        stored = "workers"
        successor = "jobs"

    with monkeypatch.context() as patch:
        patch.setattr(run_options, "SUPERSEDED_KEYS", (Renamed(),))
        with warnings.catch_warnings():
            # A mapped key is not a loss, so the read does not warn about it.
            warnings.simplefilter("error", SupersededRunOptions)
            read = registry.get_run(run_id).run_options

    assert read is not None and read["jobs"] == 3
    assert "workers" not in read


def test_a_stored_row_missing_a_field_fails_loudly(tmp_path) -> None:
    """A missing field used to become the built-in default, silently."""
    registry, run_id = _seeded_run(tmp_path)
    row = _written_row(registry, run_id)
    del row["chunk_seconds"]
    _store(registry, run_id, row)

    with pytest.raises(MalformedRunOptions) as raised:
        registry.get_run(run_id)

    assert f"run {run_id}" in str(raised.value)
    assert "chunk_seconds: Field required" in str(raised.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("jobs", "many"),
        ("jobs", "30"),
        ("resume", "yes"),
        ("chunk_seconds", "30"),
        ("backend", 12),
        ("beam_size", "5"),
    ],
)
def test_a_stored_value_the_field_cannot_read_fails_loudly(
    tmp_path, field, value
) -> None:
    """A wrong kind of value used to reach ``PipelineOptions`` unchanged.

    Read strictly, so the near misses a json-shaped guess would accept — the
    string ``"30"`` for a number, ``"yes"`` for a boolean — are refused too: the
    ticket's failure is a *mistyped* field, and a coerced one is exactly that.
    """
    registry, run_id = _seeded_run(tmp_path)
    row = _written_row(registry, run_id)
    row[field] = value
    _store(registry, run_id, row)

    with pytest.raises(MalformedRunOptions) as raised:
        registry.get_run(run_id)

    assert f"run {run_id}" in str(raised.value)
    assert f"{field}: Input should be a valid" in str(raised.value)


def test_a_stored_row_with_a_key_no_build_ever_wrote_fails_loudly(tmp_path) -> None:
    """An unknown key used to be dropped on the way in."""
    registry, run_id = _seeded_run(tmp_path)
    row = _written_row(registry, run_id)
    row["chunk_second"] = 30.0
    _store(registry, run_id, row)

    with pytest.raises(MalformedRunOptions) as raised:
        registry.get_run(run_id)

    assert "chunk_second: Extra inputs are not permitted" in str(raised.value)


def test_a_row_that_is_not_json_fails_loudly(tmp_path) -> None:
    registry, run_id = _seeded_run(tmp_path)
    with sqlite3.connect(str(registry.db_path)) as conn:
        conn.execute(
            "UPDATE pipeline_run SET run_options = ? WHERE id = ?",
            ("{backend: apple", run_id),
        )

    with pytest.raises(MalformedRunOptions) as raised:
        registry.get_run(run_id)

    assert f"run {run_id} carries options that are not JSON" in str(raised.value)


def test_the_refusal_names_the_run_it_is_about(tmp_path) -> None:
    """The queue acts on the row it refuses, so the refusal carries its ids."""
    registry, run_id = _seeded_run(tmp_path)
    row = _written_row(registry, run_id)
    meeting_id = registry.get_run(run_id).meeting_id
    del row["jobs"]
    _store(registry, run_id, row)

    with pytest.raises(MalformedRunOptions) as raised:
        registry.get_run(run_id)

    assert (raised.value.run_id, raised.value.meeting_id) == (run_id, meeting_id)


def test_a_json_integer_for_a_float_knob_reads_as_the_float_it_declares(
    tmp_path,
) -> None:
    """The row is JSON, where ``30`` and ``30.0`` are one number: the seam reads
    the value the option declares instead of refusing the encoding. Strictness is
    about the *kind* of value, not about which of two spellings of it was used."""
    registry, run_id = _seeded_run(tmp_path)
    row = _written_row(registry, run_id)
    row["chunk_seconds"] = 30
    _store(registry, run_id, row)

    assert registry.get_run(run_id).run_options["chunk_seconds"] == 30.0


def test_every_option_is_a_required_field_of_the_row() -> None:
    """The row's shape *is* the options value: every field of it, all required.

    Nothing is optional and nothing is extra, which is what makes a stored row
    missing a field a row this build cannot run rather than one it runs with a
    default nobody chose. The keys an earlier release wrote are the one exception,
    and they are optional by construction: only a row that release left behind has
    one.
    """
    row = run_options.RunOptionsRow.model_fields

    assert set(row) == {field.name for field in dataclasses.fields(PipelineOptions)}
    assert [name for name, field in row.items() if not field.is_required()] == []


def test_every_writer_round_trips_under_the_strict_read(tmp_path) -> None:
    """What this build writes is what this build reads: the strict read is not
    refusing the application's own rows.

    The enqueue path is the only writer of this column (``runs.start`` stores
    ``dataclasses.asdict`` of the resolved options), so round-tripping what it
    writes — the runner's own shape, tuples and all — is the premise strictness
    rests on.
    """
    registry, run_id = _seeded_run(tmp_path)
    written = dataclasses.asdict(
        PipelineOptions(
            backend="apple",
            model="small",
            audio_files=("/tapes/a.wav",),
            rerun_sources=("mic",),
            chunk_seconds=30,
            jobs=2,
        )
    )
    _store(registry, run_id, written)

    read = registry.get_run(run_id).run_options

    assert read == written
    assert read is not None and read["audio_files"] == ("/tapes/a.wav",)


def test_the_row_shape_is_derived_from_the_declaration_not_restated(
    monkeypatch,
) -> None:
    """One place: the declaration's row, plus the annotation the value type needs
    anyway — the two edits a knob already is (``core.options`` says so itself).

    The service layer names no knob, so a knob the declaration grows is a field
    of the row without a third edit. This grows one in those two places and
    watches the model pick it up; before the derivation, the field list would
    have had to be written out again here.
    """
    hints = get_type_hints(PipelineOptions)
    grown = dataclasses.make_dataclass(
        "PipelineOptions",
        [
            (field.name, hints[field.name], field.default)
            for field in dataclasses.fields(PipelineOptions)
        ]
        + [("synthetic_knob", int | None, None)],
    )
    monkeypatch.setattr(
        run_options, "RESOLVABLE_FIELDS", (*RESOLVABLE_FIELDS, "synthetic_knob")
    )
    monkeypatch.setattr(run_options, "PipelineOptions", grown)

    row = run_options.row_model()

    assert "synthetic_knob" in row.model_fields
    assert set(row.model_fields) == {field.name for field in dataclasses.fields(grown)}
