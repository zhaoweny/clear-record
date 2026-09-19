"""The tape-storage owner: the collision rule and the tapes directory it names.

The rule is exercised at its own seam; the two callers' end-to-end tests cover
the paths that consume it (an upload of a namesake, a re-archived meeting).
"""

from __future__ import annotations

from pathlib import Path

from clear_record.service import tapestore


def test_unique_name_takes_the_first_free_suffix() -> None:
    used = {"take.wav", "take-2.wav"}
    assert tapestore.unique_name("take.wav", taken=used.__contains__) == "take-3.wav"
    assert tapestore.unique_name("other.wav", taken=used.__contains__) == "other.wav"


def test_unique_path_never_hands_back_a_name_on_disk(tmp_path: Path) -> None:
    (tmp_path / "a.wav").write_bytes(b"one")
    (tmp_path / "a-2.wav").symlink_to(tmp_path / "gone.wav")  # a broken link
    assert tapestore.unique_path(tmp_path, "a.wav") == tmp_path / "a-3.wav"
    assert tapestore.unique_path(tmp_path, "b.wav") == tmp_path / "b.wav"
