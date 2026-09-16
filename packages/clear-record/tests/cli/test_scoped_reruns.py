"""Scoped re-runs: a glossary edit re-decodes a chosen part, not everything.

The motivating case (ADR-0018's tuning loop): a multi-hour tape, several
sources, one glossary term edited. Before scoping, that edit re-decoded *every*
chunk of *every* source. These tests pin the before/after chunk counts so a
later refactor cannot silently lose the win, prove the conservative direction (a
plausible match is never skipped), and prove that a scope which cannot be
honoured is an actionable error rather than a quiet full pass or a quiet no-op.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from click.testing import CliRunner

from clear_record.cli import stages
from clear_record.cli.cli import _build_group, _pipeline_options
from clear_record.cli.transcription import (
    ChunkReport,
    TranscriptionOptions,
    transcribe,
)
from clear_record.cli.workspace import Workspace
from clear_record.core import (
    ChunkScope,
    ScopeError,
    Segment,
    Source,
    TranscriptionResult,
)
from clear_record.engine import plan_chunks
from clear_record.providers import BackendBase, BackendInfo

# plan_chunks(8, 3, 1) == [(0,3), (2,5), (4,7), (6,8)]: four chunks per source.
CHUNK_S = 3.0
OVERLAP_S = 1.0
TAPE_S = 8.0
SAMPLES_PER_CHUNK = len(plan_chunks(TAPE_S, CHUNK_S, OVERLAP_S))
SOURCES = ("a", "b")


def _text(path, seconds: float = TAPE_S, freq: float = 220.0, sr: int = 16000) -> None:
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32), sr)


class CountingBackend(BackendBase):
    """A backend that counts decodes and echoes one fixed transcript."""

    info = BackendInfo(
        id="counting",
        vendor="test",
        frameworks=(),
        description="counts decodes",
        default_model="counting",
        parallelizable=True,
    )

    def __init__(self, text: str = "unrelated words") -> None:
        self.calls = 0
        self.text = text

    def available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):
        self.calls += 1
        return TranscriptionResult(
            source="counting",
            segments=(Segment(0.0, 0.5, self.text, "counting"),),
            language="en",
            backend="counting",
            model="counting",
            audio_duration=0.5,
        )


class Harness:
    """A two-source workspace plus a counting backend, driven through the stage."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.wd = tmp_path / "rec"
        self.wd.mkdir()
        for i, name in enumerate(SOURCES):
            _text(self.wd / f"{name}.wav", freq=220.0 + 40.0 * i)
        stages.ingest(str(self.wd), split="mix")
        self.backend = CountingBackend()
        monkeypatch.setattr(stages, "get_backend", lambda _id: self.backend)

    def run(self, **kwargs):
        """Run the stage once; return ``(segments, chunk_report, decode_count)``."""
        self.backend.calls = 0
        kwargs.setdefault("chunk_seconds", CHUNK_S)
        kwargs.setdefault("overlap_seconds", OVERLAP_S)
        stages.transcribe(str(self.wd), "counting", **kwargs)
        _, meta = Workspace.at(self.wd).load_segments()
        report = ChunkReport(**meta["chunk_report"])
        return report, self.backend.calls

    def set_glossary(self, text: str) -> None:
        Workspace.at(self.wd).glossary_path.write_text(text + "\n", encoding="utf-8")

    def put_text(self, source: str, index: int, text: str) -> None:
        """Rewrite one cached chunk body, as if the decoder had produced it.

        Provenance is untouched: the body still carries the glossary it was
        decoded under, which is the situation the near-match guard must judge.
        """
        cache = Workspace.at(self.wd).chunk_cache(source)
        cache.write_segments(index, [Segment(0.0, 0.5, text, source)])

    def patch_text(self, **text_by_chunk: str) -> None:
        for key, text in text_by_chunk.items():
            source, index = key.split("_")
            self.put_text(source, int(index), text)


# --- acceptance 1: strictly fewer chunks than today ------------------------- #
def test_a_scoped_glossary_edit_redecodes_fewer_chunks_than_an_unscoped_one(
    tmp_path, monkeypatch, capsys
) -> None:
    """The measured before/after, pinned.

    Before: a glossary edit re-decodes every chunk of every source (8 here).
    After: the same edit, scoped to one chunk of one source, re-decodes 1.
    """
    harness = Harness(tmp_path, monkeypatch)
    total = len(SOURCES) * SAMPLES_PER_CHUNK
    assert SAMPLES_PER_CHUNK == 4 and total == 8

    # The first pass: nothing cached, everything decodes.
    report, calls = harness.run()
    assert calls == total

    # A glossary edit with no scope: today's behaviour, everywhere.
    harness.set_glossary("ProjectX")
    report, calls = harness.run()
    assert calls == total, "an unscoped glossary edit still re-decodes everything"
    assert (report.redecoded, report.reused) == (total, 0)

    # The same kind of edit, scoped to one chunk of source `a` (range 5-6 s only
    # overlaps plan chunk (4, 7)).
    capsys.readouterr()  # clear the earlier passes' output
    harness.set_glossary("ProjectY")
    report, calls = harness.run(rerun_sources=("a",), rerun_range="5-6")
    assert calls == 1, "only the scoped chunk re-decoded"
    assert (report.reused, report.redecoded, report.carried_over) == (total - 1, 1, 7)
    assert report.scoped is True
    assert report.scope == "sources=a range=0:00:05-0:00:06"

    # And the run says what it cost, rather than leaving it to be inferred.
    out = capsys.readouterr().out
    assert "1 re-decoded, 7 reused" in out
    assert "7 of the reused under an earlier glossary" in out
    assert "still carry an earlier glossary" in out

    # The carried chunks are honest: an unscoped run finishes applying the
    # glossary to exactly them.
    report, calls = harness.run()
    assert calls == total - 1
    assert (report.redecoded, report.reused, report.carried_over) == (7, 1, 0)


def test_the_win_survives_a_repeat_of_the_same_scope(tmp_path, monkeypatch) -> None:
    """Re-running the same scope is free; nothing is stale any more."""
    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    harness.set_glossary("ProjectX")
    harness.run(rerun_sources=("a",), rerun_range="5-6")

    report, calls = harness.run(rerun_sources=("a",), rerun_range="5-6")
    assert calls == 0, "repeating the same scope costs nothing"
    assert (report.redecoded, report.reused, report.carried_over) == (0, 8, 7)


def test_a_plan_change_redecodes_everything_even_when_scoped(
    tmp_path, monkeypatch
) -> None:
    """A scope narrows a *glossary* re-run; it never rescues a plan change.

    The model is part of the plan, so a model change invalidates every chunk of
    every source — including the ones the scope did not select. A scope must not
    let a decoder change quietly reuse another model's output.
    """
    harness = Harness(tmp_path, monkeypatch)
    harness.run()

    report, calls = harness.run(
        model="a-different-model", rerun_sources=("a",), rerun_range="5-6"
    )
    assert calls == 8
    assert (report.redecoded, report.reused, report.carried_over) == (8, 0, 0)


# --- acceptance 2: conservatism is tested ----------------------------------- #
def test_a_plausible_match_out_of_scope_is_pulled_back(tmp_path, monkeypatch) -> None:
    """The guard widens the scope; a near-match is never skipped.

    Source `b` is out of scope, but two of its cached chunks could hold the new
    term — one literally, one a one-edit mis-spelling. Both must re-decode.
    """
    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    harness.put_text("b", 0, "we met Acme today")
    harness.put_text("b", 1, "the Acne deal")  # one substitution from "Acme"
    harness.put_text("b", 2, "unrelated words")
    harness.put_text("b", 3, "unrelated words")

    harness.set_glossary("Acme")
    report, calls = harness.run(rerun_sources=("a",))
    assert report.guard_redecoded == 2, "the near-match was not skipped"
    assert (report.redecoded, report.reused, report.carried_over) == (6, 2, 2)
    assert calls == 6


def test_an_in_scope_chunk_is_redecoded_even_when_it_already_matches(
    tmp_path, monkeypatch
) -> None:
    """Matching never short-circuits a re-decode: the texts must not save work.

    A chunk of the scoped source whose cached transcript already reads the new
    term exactly is still re-decoded, because its provenance is the old
    glossary. If matching could skip work, this run would decode 3, not 4.
    """
    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    harness.put_text("a", 0, "Acme")

    harness.set_glossary("Acme")
    report, calls = harness.run(rerun_sources=("a",))
    assert calls == SAMPLES_PER_CHUNK
    assert report.redecoded == SAMPLES_PER_CHUNK
    assert report.guard_redecoded == 0


# --- acceptance 3: the cost is reported, not assumed ------------------------ #
def test_the_run_meta_records_the_cost(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    harness.set_glossary("ProjectX")
    harness.run(rerun_sources=("a",), rerun_range="5-6")

    _, meta = Workspace.at(harness.wd).load_segments()
    assert meta["chunk_report"] == {
        "scoped": True,
        "scope": "sources=a range=0:00:05-0:00:06",
        "reused": 7,
        "redecoded": 1,
        "carried_over": 7,
        "guard_redecoded": 0,
    }


# --- acceptance 4: scoping is explicit and refuses to guess ----------------- #
def test_an_unknown_source_is_an_actionable_error(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    with pytest.raises(SystemExit) as excinfo:
        harness.run(rerun_sources=("typo",))
    message = str(excinfo.value)
    assert "typo" in message
    assert "available: a, b" in message


def test_a_range_that_covers_no_chunk_is_an_actionable_error(
    tmp_path, monkeypatch
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    with pytest.raises(SystemExit) as excinfo:
        harness.run(rerun_range="1000-2000")
    message = str(excinfo.value)
    assert "selects no chunk" in message
    assert "a=8.0s" in message and "b=8.0s" in message


def test_a_range_that_covers_no_chunk_does_not_invalidate_the_cache(
    tmp_path, monkeypatch
) -> None:
    """A refused scope must fail before the cache is touched."""
    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    before = sorted(
        p.name for p in Workspace.at(harness.wd).chunk_cache("a").directory.iterdir()
    )

    with pytest.raises(SystemExit):
        harness.run(rerun_range="1000-2000")

    after = sorted(
        p.name for p in Workspace.at(harness.wd).chunk_cache("a").directory.iterdir()
    )
    assert after == before


def test_a_scope_with_no_resume_is_a_contradiction(tmp_path) -> None:
    source = Source(id="a", path=str(tmp_path / "missing.wav"))
    with pytest.raises(ScopeError, match="resume is off"):
        transcribe(
            [source],
            CountingBackend(),
            TranscriptionOptions(scope=ChunkScope.parse(("a",), None), resume=False),
            workspace=Workspace.at(tmp_path / "w"),
        )


def test_the_transcription_module_refuses_an_unknown_source(tmp_path) -> None:
    """The error survives at the module seam, not only through the stage."""
    source = Source(id="a", path=str(tmp_path / "missing.wav"))
    with pytest.raises(ScopeError, match="not in the manifest"):
        transcribe(
            [source],
            CountingBackend(),
            TranscriptionOptions(scope=ChunkScope.parse(("typo",), None)),
            workspace=Workspace.at(tmp_path / "w"),
        )


# --- the CLI-owned scope strings follow the installed catalog ---------------- #
def test_the_cli_owned_scope_errors_are_translated(tmp_path, monkeypatch) -> None:
    """The strings ``cli/transcription.py`` owns go through ``tr()``.

    The zh_CN catalog carries all three; before the fix they were plain
    f-strings, so a Chinese user read English for every CLI-owned scope error.
    """
    from clear_record.core import i18n

    i18n.install("zh_CN")
    source = Source(id="a", path=str(tmp_path / "missing.wav"))
    with pytest.raises(ScopeError, match="重新运行范围") as err:
        transcribe(
            [source],
            CountingBackend(),
            TranscriptionOptions(scope=ChunkScope.parse(("a",), None), resume=False),
            workspace=Workspace.at(tmp_path / "w"),
        )
    assert "resume is off" not in str(err.value)

    harness = Harness(tmp_path, monkeypatch)
    harness.run()
    with pytest.raises(SystemExit) as excinfo:
        harness.run(rerun_sources=("typo",))
    assert "重新运行范围指定了清单中不存在的来源" in str(excinfo.value)
    assert "not in the manifest" not in str(excinfo.value)

    with pytest.raises(SystemExit) as excinfo:
        harness.run(rerun_range="1000-2000")
    assert "没有选中" in str(excinfo.value)
    assert "selects no chunk" not in str(excinfo.value)


# --- the CLI surface -------------------------------------------------------- #
def _parse(command: str, argv: list[str]):
    from types import SimpleNamespace

    cmd = _build_group().commands[command]
    with cmd.make_context(command, list(argv)) as ctx:
        return SimpleNamespace(**ctx.params)


def test_the_scope_flags_reach_the_run_options() -> None:
    options = _pipeline_options(
        _parse("transcribe", ["dir", "--rerun-source", "a", "--rerun-range", "5-6"])
    )
    assert options.rerun_sources == ("a",)
    assert options.rerun_range == "5-6"
    assert options.chunk_scope() is not None

    run_options = _pipeline_options(_parse("run", ["dir"]))
    assert run_options.rerun_sources is None
    assert run_options.chunk_scope() is None


@pytest.mark.parametrize("value", ["noon-18:00", "12:70-13:00", "18:00-12:30"])
def test_a_malformed_range_is_a_usage_error(value: str) -> None:
    result = CliRunner().invoke(
        _build_group(), ["transcribe", "dir", "--rerun-range", value]
    )
    assert result.exit_code == 2
    assert "--rerun-range" in result.output + result.stderr
