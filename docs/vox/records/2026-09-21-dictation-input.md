# Dictation as an input mode — owner-voice record (2026-09-21)

Status: **Open requirement, deferred to 0.4** (no release date; no ADR yet). The verbatim testimony behind
this record is personal-context material and is kept in the **private tracker** (`vox-private-records`)
rather than reproduced here; this file carries what the repo needs from it, stated from that
testimony's words. Date per the source export's own stamp — an export lags the moment the words were spoken
(see *How to read this file* in [`voice-of-owner.md`](../voice-of-owner.md)).

## What the position requires

- [REQ] **Two transcription modes, one workflow**: near-real-time while the owner speaks, and a
  re-transcription of the completed audio. The transcript is an input to an agent rather than the end
  product, and the transcript working together with the agent matters more than transcription accuracy on
  its own.
- [REQ] **Dictation is a first-class input.** The subject of a recording can be the owner himself, not only
  a meeting.
- [REQ] **The material may be sensitive, and the tool must work where it cannot leave.** Some of what is
  dictated is private in nature; capture and the agent hand-off have to be possible without the material
  leaving the environment the owner controls (`PRIVACY.md`, ADR-0006).
- [OPEN] Whether the connection to a record system outside this repo is a requirement of **clear-record**,
  of that system, or of the seam between them. What clear-record owes the seam is what is open here. The
  further framing — which models, services, users and workflows may reach which recordings, transcripts,
  agent memory and outputs, decided before anything enters an agent workflow — is the agent's proposal,
  not the owner's words. The triage questions live in the tracker's architecture lane.
- [OPEN] The shape of the three questions is unsettled: what near-real-time means against the two shipping
  backends; what a capture of the owner speaking alone is as a first-class object (a workspace kind, a run
  origin, a summary template); and what the access boundary should be for dictated material.
