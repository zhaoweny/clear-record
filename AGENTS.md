# AGENTS.md

Standing instructions for agents working in this repo. Read this first.

## Agent skills

### Issue tracker

Work is tracked as local markdown under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

> ⚙️ Agentic git workflow: main = control plane, task worktrees under `.wt/`, geometry/authority in `docs/agents/git-worktree.toml`. The workflow, review-gate, and wording skills are not vendored here — canonical copies live in the personal logbook under `50-59-engineering/57-agent-skills/` (`git-worktree`, `code-review-loop`, `proofreading`).

## Hard rules

- **Never import work/company artifacts.** This is a clean-room OSS repo. Do not
  add company source code, company prompts/specs, partner names, company
  recordings, datasets, internal docs or credentials. Proposed scope changes
  that would import such material must be rejected and escalated instead.
- **Keep the core vendor-free.** `cr-core` must never import CUDA, ROCm,
  Metal/CoreML, torch/tensorflow or a specific ASR library. Vendor things go in
  `cr-providers`, behind the `Backend` interface.
- **Preserve provenance.** Label statements FACT / VOICE / REQ / DESIGN /
  SUGGESTION / OPEN. Do not promote a suggestion or an open question to a
  requirement without owner evidence.
- **Do not add branching scripts.** Simple (branching-free) automation is a
  `just` recipe; anything with branching is a Python inline-script behind a
  pointer recipe. See the `justfile` header.
