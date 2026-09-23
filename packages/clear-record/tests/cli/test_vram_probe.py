"""VRAM-probe tests, with every machine-specific output injected.

No test here reads the real GPU, a real DRM sysfs node, a real ``nvidia-smi``
or the host's ``/proc/meminfo``: the glob result, the ``nvidia-smi`` stdout and
the meminfo file are all supplied by the test, so the suite behaves identically
on a laptop, a discrete-GPU box and a unified-memory (DGX Spark) machine.

The contract under test (the local tracker's ``hardware-backends`` lane, ticket 04):

- precedence stays ``CR_VRAM_GB`` > DRM ``mem_info_vram_total`` > ``nvidia-smi``
  > the new UMA fallback > the 8 GB floor;
- a unified-memory GPU that reports ``N/A`` falls back to system memory, so a
  ``large`` model gets the 4-way design point instead of the floor's 1 worker;
- an unprobeable machine keeps the 8 GB floor.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clear_record.pipeline import transcription
from clear_record.pipeline.transcription import (
    auto_jobs,
    detect_vram_gb,
    resolve_jobs,
)

_GIB_KIB = 1024**2  # kB per GiB, as ``/proc/meminfo`` reports MemTotal
_NVIDIA_SMI = "/usr/bin/nvidia-smi"


def _clean_vram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the operator overrides so the probes decide."""
    monkeypatch.delenv("CR_VRAM_GB", raising=False)
    monkeypatch.delenv("CR_JOBS", raising=False)


def _no_drm_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``mem_info_vram_total`` node (the amdgpu-only DRM path is absent)."""
    monkeypatch.setattr(transcription.glob, "glob", lambda _pattern: [])


def _one_drm_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gib: int) -> None:
    """One DRM card reporting ``gib`` bytes of VRAM."""
    node = tmp_path / "mem_info_vram_total"
    node.write_text(str(gib * 1024**3), encoding="ascii")
    monkeypatch.setattr(transcription.glob, "glob", lambda _pattern: [str(node)])


def _nvidia_smi(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    returncode: int = 0,
    present: bool = True,
) -> None:
    """Inject an ``nvidia-smi`` whose query prints ``stdout`` and exits ``rc``."""
    monkeypatch.setattr(
        transcription.shutil, "which", lambda _name: _NVIDIA_SMI if present else None
    )

    def _run(*args, **_kwargs):
        argv = args[0] if args else [_NVIDIA_SMI]
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(transcription.subprocess, "run", _run)


def _meminfo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, total_gib: int) -> None:
    """Inject a ``/proc/meminfo`` whose ``MemTotal`` is ``total_gib`` GiB."""
    path = tmp_path / "meminfo"
    path.write_text(
        f"MemTotal:       {total_gib * _GIB_KIB} kB\n"
        "MemFree:        1024 kB\n"
        "MemAvailable:   1024 kB\n",
        encoding="ascii",
    )
    monkeypatch.setattr(transcription, "_PROC_MEMINFO", path)


def _absent_meminfo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a non-Linux host: no ``/proc/meminfo`` to read."""
    monkeypatch.setattr(transcription, "_PROC_MEMINFO", tmp_path / "no-such-meminfo")


# --- (a) a discrete GPU reports its VRAM ----------------------------------- #


def test_discrete_nvidia_gpu_reports_its_vram(tmp_path, monkeypatch) -> None:
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _absent_meminfo(tmp_path, monkeypatch)
    _nvidia_smi(monkeypatch, "24576\n")  # MiB -> 24 GiB
    monkeypatch.setattr(transcription.os, "cpu_count", lambda: 16)

    assert detect_vram_gb() == 24.0
    # 24 GB x 0.85 / 3.7 = 5 -> clamped by the 4-way cap.
    assert auto_jobs(10, "large-v3", detect_vram_gb()) == 4


def test_drm_vram_beats_nvidia_smi(tmp_path, monkeypatch) -> None:
    """The DRM (amdgpu) probe keeps precedence over ``nvidia-smi``."""
    _clean_vram_env(monkeypatch)
    _one_drm_card(tmp_path, monkeypatch, 48)
    _nvidia_smi(monkeypatch, "8192\n")

    assert detect_vram_gb() == 48.0


def test_nvidia_smi_reports_the_largest_device(tmp_path, monkeypatch) -> None:
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _absent_meminfo(tmp_path, monkeypatch)
    _nvidia_smi(monkeypatch, "8192\n24576\n")

    assert detect_vram_gb() == 24.0


# --- (b) UMA with N/A falls back to system memory -------------------------- #


def test_uma_falls_back_to_system_memory(tmp_path, monkeypatch) -> None:
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _meminfo(tmp_path, monkeypatch, 128)
    _nvidia_smi(monkeypatch, "N/A\n")  # unified memory: no framebuffer total
    monkeypatch.setattr(transcription.os, "cpu_count", lambda: 16)

    # Half of 128 GB is claimed (the conservative UMA share)...
    assert detect_vram_gb() == 64.0
    # ...so a large model reaches the 4-way design point rather than the floor's 1.
    assert auto_jobs(10, "large-v3", detect_vram_gb()) == 4
    assert auto_jobs(10, "large-v3", 8.0) == 1


def test_uma_plain_nvidia_smi_not_supported_also_falls_back(
    tmp_path, monkeypatch
) -> None:
    """The default (non-query) display prints ``Not Supported`` on an iGPU."""
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _meminfo(tmp_path, monkeypatch, 128)
    _nvidia_smi(monkeypatch, "Not Supported\n")

    assert detect_vram_gb() == 64.0


def test_uma_claim_is_only_a_share_of_total_ram(tmp_path, monkeypatch) -> None:
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _meminfo(tmp_path, monkeypatch, 32)
    _nvidia_smi(monkeypatch, "N/A\n")

    # 32 GB of RAM is never handed to the pool as 32 GB of free VRAM.
    assert detect_vram_gb() == 16.0
    assert detect_vram_gb() < 32.0


def test_uma_without_proc_meminfo_keeps_the_floor(tmp_path, monkeypatch) -> None:
    """Off Linux there is no ``/proc/meminfo``, so the fallback does not fire."""
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _absent_meminfo(tmp_path, monkeypatch)
    _nvidia_smi(monkeypatch, "N/A\n")
    monkeypatch.setattr(transcription.os, "cpu_count", lambda: 16)

    assert detect_vram_gb() is None
    assert auto_jobs(10, "large-v3", detect_vram_gb()) == 1


# --- (c) an unprobeable machine keeps the 8 GB floor ----------------------- #


def test_unprobeable_machine_keeps_the_floor(tmp_path, monkeypatch) -> None:
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    # Even with a readable /proc/meminfo, no GPU signal means we never claim RAM
    # as VRAM -- otherwise a CPU-only Linux box would be silently over-provisioned.
    _meminfo(tmp_path, monkeypatch, 128)
    _nvidia_smi(monkeypatch, "", present=False)
    monkeypatch.setattr(transcription.os, "cpu_count", lambda: 16)

    assert detect_vram_gb() is None
    assert auto_jobs(10, "large-v3", detect_vram_gb()) == 1
    assert resolve_jobs(True, 10, 0, model="large-v3") == 1


def test_failed_nvidia_smi_query_is_not_read_as_uma(tmp_path, monkeypatch) -> None:
    """A driver hiccup with no device row is not the unified-memory signal."""
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _meminfo(tmp_path, monkeypatch, 128)
    _nvidia_smi(monkeypatch, "", returncode=6)

    assert detect_vram_gb() is None


# --- CR_VRAM_GB stays the top-precedence override -------------------------- #


def test_cr_vram_gb_overrides_every_probe(tmp_path, monkeypatch) -> None:
    _one_drm_card(tmp_path, monkeypatch, 48)
    _meminfo(tmp_path, monkeypatch, 128)
    _nvidia_smi(monkeypatch, "N/A\n")
    monkeypatch.setenv("CR_VRAM_GB", "24")

    assert detect_vram_gb() == 24.0


# --- the DoD arithmetic, stated directly ----------------------------------- #


def test_auto_jobs_uma_large_is_four_but_discrete_8gb_is_one(
    tmp_path, monkeypatch
) -> None:
    _clean_vram_env(monkeypatch)
    _no_drm_card(monkeypatch)
    _meminfo(tmp_path, monkeypatch, 128)
    _nvidia_smi(monkeypatch, "N/A\n")
    monkeypatch.setattr(transcription.os, "cpu_count", lambda: 16)

    assert auto_jobs(10, "large-v3", detect_vram_gb()) == 4
    assert auto_jobs(10, "large-v3", 8.0) == 1
