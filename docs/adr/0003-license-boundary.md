# ADR-0003 — License boundary between MIT core and vendor/copyleft dependencies

Status: active
Date: 2026-09-09

## Context

- [DECISION] ADR-0002 licenses clear-record's own code under MIT.
- [VOICE] The ASR backends must support Apple (Metal/Core ML/ANE), NVIDIA (CUDA)
  and AMD (ROCm/Vulkan) — see ADR-0005.
- [FACT] The preferred transcription stacks are permissive: `whisper.cpp` (MIT),
  `faster-whisper` (MIT), CTranslate2 (MIT); PyTorch (BSD-3) is a transitive
  dependency of the CUDA path. There is no existing requirement to aggregate a
  copyleft component in-process.

## Policy (owner text, verbatim)

> **License boundary**
>
> clear-record is MIT licensed.
>
> LGPL dependencies may be used through their supported library interfaces
> subject to their respective LGPL obligations.
>
> GPL/AGPL software must not be copied into, statically or dynamically linked
> into, or otherwise incorporated into the MIT-licensed core. Integration with
> GPL/AGPL applications should occur through clearly defined external process or
> network interfaces unless the affected component is intentionally distributed
> under a compatible copyleft license.
>
> Modifications to third-party LGPL/GPL/AGPL components retain the license
> required by their upstream projects.

## Decision

- [DECISION] Adopt the owner's license-boundary policy verbatim as the project's
  licensing rule, binding for all members of this workspace and all future code
  in this repo.
- Permissive stacks are **preferred** and are consumed behind the `Backend`
  provider interface (ADR-0005) so they are isolated from the MIT core.
- No GPL/AGPL source or artifact is vendored into this repository. If a future
  requirement needs an AGPL component (e.g. a process-based ASR server), it is
  driven over an external process/network boundary only.

## Consequences / review hook

- Any new ASR backend added to `cr-providers` must keep its stack behind the
  `Backend` interface and must not leak into `cr-core` (which must stay
  vendor-free by construction).
- Revisit if a future requirement needs AGPL code in-process — that is a
  relicense/redesign decision for the affected component; the boundary is not
  silently loosened.
