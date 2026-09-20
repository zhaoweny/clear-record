# Open-question grilling, rounds 1–4 — owner-voice record (2026-09-14)

Status: In force as answered; each answer's scoped consequence lives in an ADR named inline.
Moved here verbatim from `docs/vox/voice-of-owner.md` on 2026-09-21: no wording changed — the entry
keeps the standing positions and the index, and each section below keeps its own date.

## Open-question grilling, rounds 1–4 (2026-09-14)

Owner answers to the consolidated open questions, presented three per round. The
scoped consequences live in the trackers and ADRs; this is the intent record.

- [DECISION] **`--auto` is opt-in**; with no flags, behaviour is unchanged.
- [DECISION] A profile **tunes knobs only**; **backend selection gets its own
  `auto` knob** — owner, verbatim: *"maybe back-end itself deserve a 'auto' knob,
  but yes, profile tunes knobs"*.
- [DECISION] **Built-in profiles only** for now
  (`fast`/`balanced`/`accurate`/`custom`); config-file profiles deferred.
- [DECISION] **The default agent path is the web UI** — owner, verbatim: *"the
  default path should be clicking around on the web UI, in my opinion. then if
  they want custom command, they can talk to the agent"*. The command template is
  the advanced path; the `[agent]` config keys are plumbing, not a product surface.
- [DECISION] Transcript-check produces a **corrected revision + change list**.
- [DECISION] Archive **copies** tapes, never hardlinks.
- [DECISION] **Native first, `whisper-cli` fallback** as the platform default.
- [DECISION] **Apple-native first; Windows-native deferred** (the MSIX +
  `systemAIModels` requirement collides with the PyInstaller app).
- [DECISION] The service CLI stays **`serve` + `mcp`**; a read-only convenience
  CLI is deferred.
- [DECISION] **Push** `main` + `releases/v0.1.x` upstream.
- [DECISION] **Ship the macOS app unsigned**, keeping the documented Gatekeeper
  workaround (no Apple Developer Program spend).
- [DECISION] Flatpak keeps **`--share=network`** for first-use provisioning.

