"""Tests for the system ``whisper-cli`` AMD adapter (no GPU required).

Only the pure parsing/resolution helpers are exercised here; the live
``whisper-cli`` probe is machine-dependent and covered by the hot-test.
"""

from __future__ import annotations

import pytest

from cr_providers.backends import (
    AmdBackend,
    _resolve_ggml_model,
    _whispercli_segments,
)


def test_whispercli_segments_offsets_and_token_confidence() -> None:
    entries = [
        {
            "offsets": {"from": 0, "to": 5000},
            "text": "  hello world ",
            "tokens": [
                {"text": "[_BEG_]", "p": 0.10},
                {"text": " hello", "p": 0.80},
                {"text": " world", "p": 0.60},
            ],
        }
    ]
    segs = _whispercli_segments(entries, source="amd", language="en")
    assert len(segs) == 1
    assert segs[0].start == 0.0
    assert segs[0].end == 5.0
    assert segs[0].text == "hello world"
    assert segs[0].source == "amd"
    # Control token [_BEG_] is excluded: mean(0.8, 0.6) == 0.7.
    assert segs[0].confidence == pytest.approx(0.7)


def test_whispercli_segments_skips_empty_and_normalizes_reversed() -> None:
    entries = [
        {"offsets": {"from": 1000, "to": 2000}, "text": "   ", "tokens": []},
        {"offsets": {"from": 9000, "to": 4000}, "text": "back", "tokens": []},
    ]
    segs = _whispercli_segments(entries, source="amd", language="zh")
    assert len(segs) == 1
    assert segs[0].start == 4.0
    assert segs[0].end == 9.0
    assert segs[0].confidence is None  # no usable token probabilities


def test_resolve_ggml_model_by_name_and_path(tmp_path) -> None:
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"stub")
    assert _resolve_ggml_model("small", str(tmp_path)) == str(model)
    assert _resolve_ggml_model("ggml-small.bin", str(tmp_path)) == str(model)
    assert _resolve_ggml_model(str(model), None) == str(model)
    with pytest.raises(FileNotFoundError):
        _resolve_ggml_model("medium", str(tmp_path))


def test_amd_backend_metadata_unchanged() -> None:
    backend = AmdBackend()
    assert backend.info.id == "amd"
    assert "Vulkan" in backend.info.frameworks


def test_nvidia_and_amd_share_the_cli_adapter() -> None:
    """Both GPU families use the same process-isolated whisper-cli path."""
    from cr_providers.backends import NvidiaBackend, _WhisperCliBackend

    assert isinstance(AmdBackend(), _WhisperCliBackend)
    assert isinstance(NvidiaBackend(), _WhisperCliBackend)
    assert AmdBackend().info.parallelizable
    assert NvidiaBackend().info.parallelizable
