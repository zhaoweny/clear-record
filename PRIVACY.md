# Privacy

`clear-record` is **local-first**. There is no telemetry, no accounts, no
analytics, and no cloud transcription. Your recordings stay on the machine you
run the tool on.

This document describes what the software does. It is a statement of behaviour,
not a legal opinion. The canonical data-handling rule is
[ADR-0006](docs/adr/0006-private-data-boundary.md).

## What stays local

- **Recordings and derived artifacts.** Input audio, normalized audio, chunk
  caches, and the exported Markdown/SRT/VTT/JSON all live in the **workspace
  directory you choose** (the `directory` argument). The tool never uploads
  them.
- **Transcripts.** Everything the pipeline derives from your audio stays in that
  workspace.
- **Model weights.** The `ggml-*.bin` files are stored in your models directory
  (`--models-dir` / `CR_MODELS_DIR` / `./models`), on your disk.
- **No accounts.** Nothing to sign in to, no API key, no per-minute billing.

Recordings, derived transcripts, and downloaded model weights are
**environment-local data**: they are gitignored and are never committed to the
repository ([ADR-0006](docs/adr/0006-private-data-boundary.md)). This is a
repository rule about source control; it is not a claim about what else on your
machine can read those files, and it does not stop a workspace on a synced
drive from syncing. Point the workspace somewhere you control.

## The one network activity: first-use model download

Processing itself needs no network. The **only** network request the tool makes
is a one-time **model download** when a requested model is not already on disk:

- Default source: `https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-<name>.bin`
  (the Hugging Face repository `ggerganov/whisper.cpp`).
- Override the base with **`HF_ENDPOINT`** — any Hugging Face–compatible mirror
  or self-hosted endpoint works (for restricted networks, e.g.
  `HF_ENDPOINT=https://hf-mirror.com`). The default is
  `https://huggingface.co`.
- The file is streamed over the configured endpoint (HTTPS by default) and
  written to your models directory. An interrupted or failed download removes
  its temporary file; if the machine is offline, the tool stops with an
  actionable `hf download …` pre-fetch message.
- If the model is already present, no request is made. To stay fully offline,
  pre-fetch the model (for example with `hf download`) or point the tool at an
  existing file, and leave the network alone.

This download is a **provisioning** step, not an execution dependency: once the
model is on disk, transcription runs with no network access at all.

Because it is a normal HTTPS request, the operator of the endpoint you use (by
default, Hugging Face) can observe the connection — for example your IP address
and the model you requested — and that operator's own policies apply. If that
matters to you, use `HF_ENDPOINT` to point at a mirror or endpoint you trust.
`clear-record` sends no analytics and no additional identifying payload beyond
the HTTP request itself.

## External tools run locally

Some stages shell out to external programs on your `PATH`; they run as local
processes and process your local files:

- **`whisper-cli`** (from `whisper.cpp` / `ggml`) performs transcription. It is
  a separate project, not maintained here.
- **`ffmpeg`** is used as a local fallback for decoding audio formats that the
  built-in decoder does not cover (e.g. m4a/aac).

`clear-record` invokes these locally and does not configure them to contact the
network. Whether a particular build of those external tools does anything else
is outside this project's control — see each project's own documentation.

## What this project does not do

- No telemetry, usage reporting, or "phone home".
- No analytics or crash reporting.
- No account, sign-in, or subscription.
- No cloud or third-party transcription service.
- No uploading of recordings, transcripts, or model weights.

## A neutral tool — your responsibility

Automatic speech recognition is **inherently privacy-sensitive**: it turns
people's voices into searchable text, and the people in a recording may not have
chosen to be recorded or transcribed.

`clear-record` is a **neutral, general-purpose tool**. It has no built-in purpose
and does not restrict what you point it at. **You may use it for your own
purposes** — and you are **solely responsible** for doing so lawfully and
ethically, including:

- having whatever **consent** your situation requires before recording or
  transcribing other people;
- complying with the laws that apply to you (wiretapping/consent, data
  protection, and any workplace or confidentiality rules);
- deciding what to keep, share, or delete.

The project and its authors take **no position on, and no responsibility for, how
the software is used or for any consequences of that use.** This section is a
plain-language statement for the privacy-sensitive case, not legal advice; the
[MIT license](LICENSE) already disclaims warranties and liability to the extent
the law allows.

## Reporting a privacy concern

Open a GitHub issue on the repository:
<https://github.com/zhaoweny/clear-record/issues>. For anything that should not
be public (for example a security-relevant data-handling bug), use the private
route in [`SECURITY.md`](SECURITY.md) instead. Do not attach private recordings
or transcripts — use synthetic audio (`clearrecord synth`) where a reproduction
is needed.
