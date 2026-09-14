# Security Policy

`clear-record` is a local-first, pre-1.0 open-source project maintained on a
best-effort basis. There is no security team and no bug bounty. If you find a
vulnerability, please report it privately as described below rather than opening
a public issue with exploit details.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting**, which opens a private Security
Advisory visible only to the reporter and the maintainer:

1. Go to the repository's **Security** tab:
   <https://github.com/zhaoweny/clear-record/security>
2. Click **Report a vulnerability** (or open
   <https://github.com/zhaoweny/clear-record/security/advisories/new>).
3. Describe the issue. The advisory is private until it is published.

If private reporting is unavailable, open a **minimal** public issue that only
asks for a private contact channel — do not put the vulnerability, a proof of
concept, or affected input into the public issue text.

Please do **not** send a report by attaching a private recording, transcript, or
model weight. Use synthetic audio (the CLI ships a `clear-record synth` command
that generates test material) or a file you are free to share.

## Supported versions

This project is **pre-1.0**. The `0.1` line is tagged — `v0.1.1` is the current
stable release, and `v0.1.0` was a source-only tag. Development continues on
`main`, the `0.2` trunk.

| Version | Supported |
| --- | --- |
| `main` (the `0.2.x` development trunk) | ✅ |
| `releases/v0.1.x` (the `0.1` maintenance line) | ✅ |
| Older releases, older commits and pre-release builds | ❌ |

Fixes land on `main`, and a `0.1.x` patch release is cut from
`releases/v0.1.x`. There are no backports to older commits. Because the project
is pre-1.0, a fix may also change or document behaviour without a deprecation
period.

## Scope

**In scope** — this repository's own code and packaging:

- the `clear-record` CLI command (the `clear_record.cli` layer);
- the `clear_record.core`, `clear_record.engine` and `clear_record.providers`
  layers of the single `clear-record` dist;
- the build, test and CI configuration in this repository;
- unexpected behaviour triggered by processing untrusted local input (for
  example a crafted audio file, path or glossary) that escapes what the
  documented behaviour would lead you to expect.

**Out of scope** — report these to their own projects:

- **`whisper.cpp` / `ggml`** (the transcription engine and its GPU plugins) and
  **`ffmpeg`** are separate external projects that `clear-record` invokes as
  local processes. They are not maintained here; report issues to
  [`ggml-org/whisper.cpp`](https://github.com/ggml-org/whisper.cpp) and
  [ffmpeg.org](https://ffmpeg.org) respectively.
- **Model weights.** The `ggml-*.bin` files are downloaded from
  [`ggerganov/whisper.cpp`](https://huggingface.co/ggerganov/whisper.cpp) on
  Hugging Face, not authored here. `clear-record` checks the sizes it downloads
  against SHA-256 digests pinned in `clear_record.providers.ggml_hashes` and
  discards a mismatch (`CR_MODEL_CHECKSUM=off` opts out); that guards the
  transport, but the *contents* of a weight file remain upstream's to fix.
- **Third-party Python dependencies** (`numpy`, `soundfile`, and the dev-only
  `pytest`/`ruff` — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)).
  Report these upstream; if one affects `clear-record`, we are happy to
  coordinate.
- Issues that require a compromised machine, a malicious backend binary, or
  physical access.
- GPU driver, OS, or hardware issues.

## What to include

A useful report contains:

- the `clear-record` version or the commit SHA (`git rev-parse --short HEAD`),
  and how it was installed (`uv run` from a checkout, etc.);
- platform and architecture (OS/version), and the backend in use (`apple`,
  `nvidia`, `amd`, or CPU/none);
- the exact command(s) and a minimal, self-contained reproduction;
- expected vs. actual behaviour, and the security impact;
- any proof of concept, with private audio/transcripts redacted.

## What to expect

- **Best effort, no bounty.** This is a personal open-source project, not a
  funded security program; response times are not guaranteed and there is no
  reward program.
- **Acknowledgement and coordination.** We aim to acknowledge a private report
  and work with you on a fix and a coordinated disclosure timeline. Please give
  us a reasonable chance to ship a fix before publishing.
- **Credit.** We are glad to credit you in the advisory or release notes unless
  you prefer to stay anonymous.

This document is a policy for reporting, not a legal opinion or contract.
