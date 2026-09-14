# Contributing to clear-record

Thanks for your interest. clear-record is a local-first, open-source multitrack
transcription and record-reconstruction pipeline. This file covers the
mechanics; [`AGENTS.md`](AGENTS.md) holds the standing rules that humans and AI
agents follow.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — workspace and dependency tooling.
- [just](https://github.com/casey/just) — the recipe runner.
- Python 3.14 locally (`.python-version`); the project requires `>= 3.12`. `uv`
  installs the pinned interpreter for you.

No GPU, no `whisper-cli` and no model weights are needed to build or test: the
suite fakes the platform and `subprocess` calls, so `just verify` is green on a
plain Linux CI runner (see [`.github/workflows/verify.yml`](.github/workflows/verify.yml)).

## Clone and verify

```sh
git clone https://github.com/zhaoweny/clear-record.git
cd clear-record
just verify
```

`just verify` is the gate: `uv sync --all-packages --locked`, then `ruff check`,
`ruff format --check`, then the pytest suite. Run it before every commit.

Useful recipes:

```sh
just                # list recipes (the default)
just test           # pytest
just lint           # ruff check
just format         # ruff format (in place)
just format-check   # ruff format --check
just web-assets     # rebuild the console's committed CSS/JS (needs bun)
just i18n-extract   # merge new source strings into the .po catalogs (needs Babel)
just i18n-compile   # compile each .po into the committed .mo (needs Babel)
just i18n-check     # catalog freshness guard (its own CI job)
```

## Guidelines

- **Keep work/company material out of the repository.** Do not add company
  source code, prompts/specs, partner names, internal docs or credentials, and
  don't commit private recordings. Recordings, derived transcripts and model
  weights are environment-local data — gitignored, never committed (ADR-0006).
- **Keep the core vendor-free.** `clear_record.core` must never import CUDA,
  ROCm, Metal/Core ML, torch/tensorflow or a specific ASR library;
  `clear_record.engine` may use numpy/soundfile but no vendor/ASR code. Vendor
  stacks live in `clear_record.providers`, behind the `Backend` interface. This
  layering and the vendor-free core are enforced by
  `packages/clear-record/tests/test_layering.py` (ADR-0012).
- **Preserve provenance.** Label statements `FACT` / `VOICE` / `REQ` / `DESIGN`
  / `SUGGESTION` / `OPEN`. Do not promote a suggestion or an open question to a
  requirement without owner evidence.
- **Mark user-facing strings with `tr("…")`** (message IDs are the English
  strings, so the English default is unchanged). After adding one, run
  `just i18n-extract` then `just i18n-compile` and commit the regenerated
  catalogs. Logs, the JSON API, exports and the diagnostics bundle are **never**
  translated. See [`docs/i18n.md`](docs/i18n.md).

## Do not add branching scripts

Simple automation (no branching statement) belongs in the [`justfile`](justfile)
as a recipe. Anything with branching belongs in a Python inline-script (a
PEP-722/PEP-723 `# /// script` block) run by a **pointer** recipe, so `just`
stays the single entry point for automation and chaining. The `justfile` header
spells this out.

## Tests

Tests live under `packages/clear-record/tests/` (organized by layer) and run with
pytest:

```sh
just test                                                             # everything
uv run --all-packages pytest packages/clear-record/tests/providers    # one layer
uv run --all-packages pytest -k apple                                 # one pattern
```

Add a test for every behaviour change. Hardware-dependent behaviour must be
faked (platform, `subprocess`) so the suite stays green without a GPU.

## A note on AI contributions

AI agents contribute heavily to this project. That is fine — what matters is
the result: commits that are coherent and reviewable, with tests that prove the
behaviour. Reviewers judge the diff, not who or what typed it.

## Community & policies

- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — the Contributor Covenant v2.1 and
  how to report unacceptable behaviour.
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability **privately**, and
  what is in scope.
- [`PRIVACY.md`](PRIVACY.md) — the local-first data story: no telemetry, no cloud
  processing, and the single first-use model download.
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — dependency licenses and
  the [ADR-0003](docs/adr/0003-license-boundary.md) copyleft boundary.

When you open an issue or a pull request, the templates under
[`.github/`](.github/) ask for the details a reviewer needs (platform, backend,
and the status of `just verify`). **Never attach private audio, transcripts, or
model weights** — use synthetic material (`clear-record synth`) for a
reproduction.

## License

By contributing, you agree that your contributions are licensed under the
repository's [MIT license](LICENSE). The MIT license covers this repository's own
code only; see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
