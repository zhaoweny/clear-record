# cr-core

Backend-agnostic application core for **clear-record**. This package must stay
free of any vendor-specific or ML-framework-specific code: no CUDA, ROCm,
Metal/CoreML, torch, tensorflow or a specific ASR library here. It owns the
domain model and the *shape* of the pipeline.

- Pipeline phases (`cr_core.pipeline.Step`): `ingest → align → transcribe →
  reconcile → export`.
- Observation-first domain idea: store observations (timestamp, clock_domain,
  source, entity_hint, measurement, confidence, provenance) and derive
  transcript segments, speaker turns, decisions and action items afterward.

This is the package the other members depend on; they must not drag it toward a
particular vendor. See `docs/architecture.md` and ADR-0004/0005.
