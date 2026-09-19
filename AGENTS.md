# AGENTS.md

Standing instructions for agents working in this repo. Read this first.

## Agent skills

### Issue tracker

Work is tracked on the owner's private Gitea instance (reachable only over the tailnet): tickets are issues, specs are wiki pages. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

> ⚙️ Agentic git workflow: main = control plane, task worktrees under `.wt/`, geometry/authority in `docs/agents/git-worktree.toml`. The workflow, review-gate, and wording skills are not vendored here; keep them in your own local agent setup.

## Hard rules

- **Never import work/company artifacts or private recordings.** This is a
  clean-room OSS repo. Do not add company source code, company prompts/specs,
  partner names, company recordings, datasets, internal docs or credentials. And
  **never commit user recordings, derived transcripts, or downloaded model
  weights** — they are environment-local data (ADR-0006). Proposed changes that
  would commit such material are rejected and escalated.
- **Keep the core vendor-free.** `clear_record.core` must never import CUDA,
  ROCm, Metal/CoreML, torch/tensorflow or a specific ASR library.
  `clear_record.engine` may use numpy/soundfile but no vendor/ASR code. Vendor
  things go in `clear_record.providers`, behind the `Backend` interface. The
  layering and the vendor-free core are enforced by
  `packages/clear-record/tests/test_layering.py` (ADR-0012).
- **Preserve provenance.** Label statements FACT / VOICE / REQ / DESIGN /
  SUGGESTION / OPEN. Do not promote a suggestion or an open question to a
  requirement without owner evidence.
- **Do not add branching scripts.** Simple (branching-free) automation is a
  `just` recipe; anything with branching is a Python inline-script behind a
  pointer recipe. See the `justfile` header.
