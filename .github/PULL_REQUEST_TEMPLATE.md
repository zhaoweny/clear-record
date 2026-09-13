# Pull request

## What and why

<!-- What does this change, and why? Link the issue/ticket it closes, if any. -->

Closes #

## How was it verified?

<!-- Commands you ran, tests you added, and anything a reviewer should re-check. -->

- `just verify` status: <!-- green locally / not run (say why) -->

## Checklist

- [ ] `just verify` is green (`uv sync --all-packages --locked` +
      `ruff check` + `ruff format --check` + pytest).
- [ ] I added or updated a test for the behaviour change.
- [ ] I did **not** include private recordings, derived transcripts, or model
      weights (environment-local data —
      [ADR-0006](https://github.com/zhaoweny/clear-record/blob/main/docs/adr/0006-private-data-boundary.md)).
- [ ] I did **not** import work/company artifacts or private material
      (clean-room —
      [AGENTS.md](https://github.com/zhaoweny/clear-record/blob/main/AGENTS.md)).
- [ ] Provenance labels (`FACT` / `VOICE` / `REQ` / `DESIGN` / `SUGGESTION` /
      `OPEN`) are preserved where docs changed.
- [ ] No branching was added to the `justfile` (see
      [CONTRIBUTING.md](https://github.com/zhaoweny/clear-record/blob/main/CONTRIBUTING.md)).
- [ ] Docs were updated if the change is user-visible.
