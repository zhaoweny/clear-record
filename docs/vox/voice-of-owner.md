# Voice of Owner — clear-record

The authoritative recorded owner intent. A `VOICE` entry in
`docs/architecture.md` or an ADR should trace back to a line here (or to a dated
ADR). These are the owner's own words/positions, distilled from the original
concept (see `docs/architecture.md` §9).

## Project framing

- The original concept was **"from many recordings to one clear record"** — a
  post-processing system that ingests several imperfect recordings of one event
  and produces one reconstructed, attributable record. (Also: *"reconstruct the
  record."*)
- The natural surface is a CLI with one subcommand per stage:
  `clearrecord ingest | align | transcribe | reconcile | export`.

## Scope / boundaries

- **Local-first, open-source, work-unrelated.** Do not import work/company
  code, assets, credentials, recordings, datasets, partner names or internal
  docs into the repo. (Provenance note; not a legal opinion.)
- **Define it from a generic public problem statement**, not from
  "recreate what was built at work." Keep Git history from day one.
- **Offline and subscription-free**: own the hardware and models; no cloud
  cost; no dependence on a third-party service continuing to exist.

## Backends

- **Support all three major desktop compute families** behind one interface,
  inspired by the observation (Marco Arment / Overcast) that Mac frameworks are
  excellent for on-device transcription: **Apple** (Metal / Core ML / ANE),
  **NVIDIA** (CUDA), **AMD Radeon** (ROCm / Vulkan). No single-vendor lock-in.

## Reliability

- Ingestion and processing must be **timestamped and chunked/durable**; the
  pipeline must be **resumable**, so workstation/OS instability and multi-GB
  recordings do not destroy progress.

## What is deliberately NOT part of the open repo

The exotic, invention-grade ideas — distributed-transmitter spatial
arrays/beamforming, camera fusion, relative 3-D speaker localization
(acoustic triangulation, DOA/TDOA fusion), cross-device clock sync as a product
feature, network-attached world-reconstruction pods — are **not** in scope for
this repository. See `docs/architecture.md` §7.

## Command naming (2026-09-13)

- The CLI **command** is spelled `clear-record`, not `clearrecord`: *"the cli
  reads as 'clear-record' instead of 'clearrecord'"*. This supersedes the
  `clearrecord` spelling in "Project framing" above; it concerns the command
  only — the distribution name (`cr-cli`) is unaffected (ADR-0009).
