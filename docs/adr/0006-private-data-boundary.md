# ADR-0006 — Recordings & model weights are environment-local, never committed

Status: active
Date: 2026-09-10

## Context

- [VOICE] The owner will feed *private* ("off the record") recordings into the
  pipeline to calibrate the model. Those are **never intended to be known by
  anyone** and must never leave the machine in a published form.
- [VOICE] Model weights are large, per-machine artifacts; downloading them is an
  environment action, not a repository concern.
- [DECISION] ADR-0001: clean-room OSS repo; do not import work/company material.
- [FACT] The repo is intended for public release (MIT), so anything committed is
  public by construction.

## Decision

- [DECISION] **Recordings, derived transcripts, and downloaded model weights are
  environment-local data.** They are never committed to the repository.
- The `.gitignore` excludes `recordings/`, `data/`, `models/`, `cache/`,
  `artifacts/`, audio/video/ML file extensions, and derived `.srt`/`.vtt`.
- The `calibrate`/`run` workflows write everything into a **workspace directory**
  (e.g. `recordings/<name>/`) that lives under a gitignored path; the CLI never
  writes into a committed location.
- [DECISION] This is scoped and documented as a *data-handling* rule. It does
  not make any legal claim about the recordings' ownership or sensitivity beyond
  "keep them out of the repository."

## Rationale

- A public MIT repo must never carry the owner's private material; git history
  makes accidental commits effectively public and durable.
- Keeping large audio/model artifacts out of git also keeps the repo small and
  clone-friendly.

## Consequences / review hook

- An `AGENTS.md` hard rule mirrors this: any proposed change that would commit a
  recording, dataset, model weight or derived private transcript is rejected and
  escalated.
- Revisit only if the owner later wants a committed, synthetic (non-private)
  sample fixture — that is a different, deliberate decision.
