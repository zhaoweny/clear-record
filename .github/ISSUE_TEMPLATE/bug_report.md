---
name: Bug report
about: Report something that is broken or behaves unexpectedly
labels: needs-triage
---

<!--
The `labels:` line above applies only if a `needs-triage` label exists in the
repository (see `docs/agents/triage-labels.md`); remove it if it does not.

⚠️ Do not attach private recordings, transcripts, or model weights. Use
synthetic audio (`clearrecord synth`) or a file you are free to share, and
redact anything sensitive from logs.
-->

## What happened

<!-- A clear and concise description of the bug. -->

## Steps to reproduce

1.
2.
3.

**Command(s):**

```sh
# paste the exact command(s)
```

## Expected vs. actual

- **Expected:**
- **Actual:**

## Environment

- `clear-record` version or commit (`git rev-parse --short HEAD`):
- Install method (`uv run` from a checkout, etc.):
- OS / architecture:
- Backend: `apple` / `nvidia` / `amd` / none (CPU) — and, if relevant, the
  `ggml` plugin (`ggml-metal`, `ggml-cuda`, `ggml-vulkan`, `ggml-hip`):

## `just verify` status

<!-- Does the repository's own gate pass on your machine? Paste the result or say
     "not run" and why. -->

## Logs / output

<!-- Redact private audio, transcripts, names, and paths. `clearrecord synth`
     can generate shareable test material. -->

```text

```
