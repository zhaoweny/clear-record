"""Transcript evaluation helpers used by the `calibrate` command.

These compare an ASR transcript against a reference transcript. For Latin-script
text we tokenize on whitespace; for CJK text we tokenize per character (no
reliable word segmentation without a dictionary). The error metric is a Levenshtein
edit distance over the token sequences, reported as an error rate (0.0 = perfect).
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def _tokenize(text: str) -> list[str]:
    text = " ".join(text.split()).lower()
    if _CJK.search(text):
        # keep CJK chars as tokens, treat runs of non-CJK as tokens
        tokens: list[str] = []
        for run in re.findall(
            r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+|[^\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+",
            text,
        ):
            if _CJK.match(run):
                tokens.extend(list(run))
            elif run.strip():
                tokens.append(run.strip())
        return tokens
    return text.split()


def levenshtein(a: list[str], b: list[str]) -> int:
    """Classic DP edit distance over token sequences."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(
                prev[j] + 1,  # delete
                cur[j - 1] + 1,  # insert
                prev[j - 1] + (0 if ca == cb else 1),  # replace/substitute
            )
        prev = cur
    return prev[len(b)]


def error_rates(reference: str, hypothesis: str) -> dict[str, float]:
    """Return ``wer`` and ``cer``-style error rate + similarity.

    ``wer`` uses word/character tokens (see :func:`_tokenize`); ``similarity`` is
    a ``1 - error`` score in [0, 1].
    """
    ref_tokens = _tokenize(reference)
    hyp_tokens = _tokenize(hypothesis)
    err = levenshtein(ref_tokens, hyp_tokens)
    denom = max(1, len(ref_tokens))
    wer = err / denom
    return {"wer": round(wer, 4), "similarity": round(1.0 - wer, 4)}


__all__ = ["error_rates", "levenshtein", "_tokenize"]
