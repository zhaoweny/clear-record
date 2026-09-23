"""Pinned SHA-256 digests for the ``ggml-*.bin`` models clear-record downloads.

The first-use download in :mod:`clear_record.providers.backends` already has
HTTPS, a single-flight lock and an atomic rename — but none of those notices a
*complete* file of the wrong bytes. A compromised mirror, a corrupted CDN object
or a body truncated to a plausible length would all rename into place and then
be handed to ``whisper-cli``. This module is the missing integrity check: a
small table of digests pinned in the source tree, compared against the
downloaded file **before** it is renamed onto its final path.

**Why a shipped table, not a fetched ``.sha256`` sidecar.** A sidecar travels
over the same connection, from the same host, as the bytes it describes, so it
catches a truncated body but not a host that serves a substituted model *and*
its matching sidecar. A digest pinned in the package moves the trust anchor to
the artifact the user installed rather than the mirror it happens to download
from. It also costs no extra request and adds no failure mode for a mirror that
publishes no sidecar at all. The trade-off is coverage: only names listed here
can be checked.

The table covers the sizes ``clear-record`` selects for itself (the ladder in
``clear_record.pipeline.auto``) plus the ``.en`` variants users name explicitly. A
name with **no** entry — an arbitrary ``ggml-*.bin`` passed to ``--model``, or
an unusual quantization — is not an error: there is no digest to check it
against, so the download proceeds unchecked. This is defence in depth on the
path the tool picks, not a gate on every name a user can invent.

**Provenance.** Each value is the Hugging Face LFS object id for the
``ggerganov/whisper.cpp`` ``main`` revision (2026-09-14). For an LFS file the
object id **is** the SHA-256 of the file content, so the digests were read from
the repository's tree metadata rather than derived by downloading gigabytes.
Every entry was re-checked against that metadata (through the ``hf-mirror.com``
mirror of the same tree API) and matches; the identity of ``ggml-tiny.bin`` was
also confirmed end to end (fetched from ``resolve/main``, 77,691,713 bytes,
hashes to the pinned ``be07e0…``). Refresh by reading ``lfs.oid`` for each
``ggml-*.bin`` from
``https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/main``.

Set ``CR_MODEL_CHECKSUM`` to ``off`` (also ``0``/``false``/``no``,
case-insensitive) to skip the check — for a mirror or self-hosted ``HF_ENDPOINT``
that serves a different but wanted file under a pinned name. Any other value,
**including unset**, keeps it on, so a typo fails in the stricter direction.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from clear_record.core.i18n import deferred

#: The environment switch for the integrity check (the ``CR_*`` convention).
ENV_MODEL_CHECKSUM = "CR_MODEL_CHECKSUM"

#: Values of :data:`ENV_MODEL_CHECKSUM` that turn the check off.
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

#: SHA-256 of each model file, keyed by the name ``_resolve_ggml_model`` resolves
#: to (``ggml-<size>.bin``). The one home for the hashes: the download path reads
#: this table and the tests override entries in it, so a digest is never restated
#: in a second place that could drift.
GGML_MODEL_SHA256: dict[str, str] = {
    # The multilingual sizes `clear_record.pipeline.auto` can recommend.
    "ggml-tiny.bin": "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
    "ggml-base.bin": "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    "ggml-small.bin": "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    "ggml-medium.bin": "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
    "ggml-large-v3.bin": "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
    # The English-only variants users pass by name (`--model small.en`).
    "ggml-tiny.en.bin": "921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f",
    "ggml-base.en.bin": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
    "ggml-small.en.bin": "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
    "ggml-medium.en.bin": "cc37e93478338ec7700281a7ac30a10128929eb8f427dda2e865faa8f6da4356",
}


class ModelChecksumError(RuntimeError):
    """A downloaded model did not match its pinned SHA-256 digest.

    ``str(exc)`` is the English message. ``msgid`` and ``params`` carry the
    **stable message ID plus its parameters**, so a presentation boundary can
    render it in the user's locale with ``tr(exc.msgid, **exc.params)`` — the
    same contract as ``service.managed.UploadRejected``; a composed f-string
    could not be translated.
    """

    def __init__(self, msgid: str, **params: object) -> None:
        self.msgid = msgid
        self.params = params
        super().__init__(msgid.format(**params) if params else msgid)


def checksum_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether a downloaded model is checked against its pinned digest.

    Disabled only by an explicit falsy value of :data:`ENV_MODEL_CHECKSUM`; a
    blank, a typo or an unset variable leaves the check on. ``environ`` is
    injectable so the switch is testable without touching the process
    environment.
    """
    env = os.environ if environ is None else environ
    raw = env.get(ENV_MODEL_CHECKSUM)
    return raw is None or raw.strip().lower() not in _DISABLED_VALUES


def expected_sha256(
    name: str, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """The pinned digest for model file ``name``, or ``None`` when unchecked.

    ``None`` means there is nothing to check: the check is switched off, or the
    name has no pinned digest (a custom model). It never means "matched".
    """
    if not checksum_enabled(environ):
        return None
    return GGML_MODEL_SHA256.get(name)


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 of ``path``, streamed in blocks (models are GB-scale)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_model_sha256(
    name: str,
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Check a downloaded model file against its pinned digest.

    Returns ``True`` only when a pinned digest existed and ``path`` matched it;
    ``False`` when there was nothing to check. Raises :class:`ModelChecksumError`
    on a mismatch — the caller is responsible for discarding the file.
    """
    expected = expected_sha256(name, environ=environ)
    if expected is None:
        return False
    actual = sha256_file(path)
    if actual != expected:
        # A plain comparison: a digest is public, so there is no timing secret.
        raise ModelChecksumError(
            deferred(
                "the downloaded model {name} failed its SHA-256 check "
                "(expected {expected}, got {actual}); it was discarded. Retry "
                "the download, or set {env}=off to accept the endpoint's bytes "
                "unchecked."
            ),
            name=name,
            expected=expected,
            actual=actual,
            env=ENV_MODEL_CHECKSUM,
        )
    return True


__all__ = [
    "ENV_MODEL_CHECKSUM",
    "GGML_MODEL_SHA256",
    "ModelChecksumError",
    "checksum_enabled",
    "expected_sha256",
    "sha256_file",
    "verify_model_sha256",
]
