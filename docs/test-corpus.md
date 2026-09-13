# Test corpus & public references

The owner-sanctioned, **clean-room-safe** (public, non-work) reference material
and the calibration strategy for `clear-record`.

## Why synthesize the `align` badness

`[VOICE: owner]` "we are hard to find any real 4 channel pre-mixed source
of bad-ness." — genuinely bad *published* multi-track is scarce, and when it
exists there is usually **no clean correct answer** to score recovery against
(producers mix down / clean before publishing).

`[FACT]` Therefore the **`align`** test set is **synthesized**: clean
CC-licensed speech → room impulse response + noise + per-track time offset +
clock drift + gain/FR mismatch + dropouts → the exact "bad 4-channel pre-mix"
you want, **while retaining the clean aligned transcript as ground truth**. This
is the only reliable way to get the desired badness *with a known-correct
answer*.

This is implemented by `clearrecord synth` (see `cr_engine.synth`): it builds a
clean multi-speaker scene, degrades it per device, and writes an exact
`ground_truth.json`. `just verify` proves `align` recovers the true offsets.

### Voice modes: noise (default) vs voiced

`[FACT]` `cr_engine.synth` renders each speaker stem in one of two modes:

- **noise** (default, `voiced=False`) — aperiodic, speech-shaped noise. This is
  the default because the `align` and energy tests do not depend on pitch, and
  changing it would move the event-timeline / seed contract.
- **voiced** (`voiced=True`, with a per-speaker `f0_hz` and a
  `f0_gap_semitones` spacing) — a harmonic glottal source: a harmonic stack with
  natural roll-off, a syllabic envelope and a mild timbre lowpass. This is the
  mode for **pitch / F0** experiments; the noise default is byte-for-byte
  unchanged.

`make_scene`, `make_speaker_stems` and `make_crosstalk_scene` take the same
opt-in keywords.

## The three public anchors

Source material falls into two buckets:

- **multi-device per-mic** → exercises the **`align`** failure mode (Anchor 2);
- **single mixed stream** → **transcribe / reconcile** realism (Anchors 1 & 3).

### Anchor 1 — a public vlog (clean audio source)

`[VOICE: owner]`
["This One Small Thing in China Made Me Question Life in the UK"](https://www.youtube.com/watch?v=JVmZoPnHZ88)

> "these are 2 couples and they are doing vlog for recording. from time to time
> they would have their video auto centered or separated to left and right
> channels. not sure what we are aiming for but it's clear audio sauce."

`[FACT]` The "auto-centred / left-right separated" behaviour is a *video*
phenomenon; the value taken is the **clean audio** as a transcription / test
source. `[OPEN]` Whether it is a multi-source alignment case or just a
transcription corpus is undetermined.

### Anchor 2 — four-person panel, 4-channel DJI mics (multi-source align)

`[VOICE: owner]`
["全新栏目上线！车评里不能讲的全藏在这里了"](https://www.bilibili.com/video/BV1kCud6XEST/)
by **大家车言论** (car-review KOL panel).

> "here's a 4 person production and they have clear audio via 4 channel dji
> mics. so the mic closer to the speaker would be the best - less to recovery and
> less to worry."

`[FACT]` Four people, each with a dedicated mic, one production → **each
speaker's nearest mic is the cleanest source** → speech needs the least
separation/recovery. This is a genuine **multi-source `align`** case and
motivates the **closest-mic-wins** rule in `reconcile` (per-channel ⇒
`speaker ≈ source`, minimising diarization work).

**Cross-talk and the room reference.** `[FACT]` Close lavaliers do not isolate
one voice: each mic still picks up its neighbours, so a "closest mic" can carry a
segment it does not own and closest-mic-wins misattributes it. `[REQ]` **Whenever
per-mic sources may bleed, also record a mixed/room reference.** The room mic is a
neutral, speaker-independent witness: it shows that a segment was spoken even
when no identified per-mic channel is loud in that window, so attribution can
keep the incoming speaker instead of guessing a bleed channel. This is implemented
by `clearrecord attribute` → `cr_engine.attribute.attribute_segments` (relative,
gain-normalized per-source energy). The room is excluded from the candidate set
even when it is also listed in the manifest, so it is never emitted as a speaker.
The gate is calibrated for a room reference that carries every speaker at a
comparable level, for cross-talk down to about −6 dB (weaker bleed is easier), and
for **non-overlapping** speech — overlapping utterances blur the level comparison,
so the fence is scoped to that regime. A room mic far below the per-mic level can
leave a covered speaker uncorrected, in which case attribution conservatively keeps
the incoming speaker. The badness is synthesized with exact ground truth by
`cr_engine.synth.make_crosstalk_scene` (`non_overlapping=True` for the calibrated
regime). `[FACT]` A synthetic ground-truth study found **per-source level
normalization** does the work, not pitch: stateless closest-mic collapses to ~0.5
accuracy once the hot device's bleed wins, while normalizing each source against
its own level stays 0.94–0.995. A **constant** gain imbalance is handled by a
single static correction; the `--window-s` rolling window (≈15 s) only earns its
keep when the gain **drifts** (a mid-tape step: 0.51 → 0.94–0.986). The path emits
a calibrated confidence and uses **no pitch/F0 cue** (a fixed, well-calibrated F0
cue still lost accuracy). **Honest limit:** the evidence is synthetic
additive/delay-free cross-talk; intrinsic speaker-level imbalance (not a mic gain)
is a regime per-source normalization cannot win, and real-tape validation still
needs independent labels.
`[OPEN]` Whether a room reference can *name* a speaker, rather than gate/fall
back, remains undetermined; here it is a witness, never a speaker candidate.

### Anchor 3 — John Gruber's *The Talk Show* (live, single mixed stream)

`[VOICE: owner]` "I'm talking John Gruber's *The Talk Show*, live from WWDC."

`[FACT]` A real, **live** podcast master — a **single mixed stream**, so a
**transcribe / reconcile** realism source, **not** a multi-device `align`
source. It also motivates the synthesize strategy above.

## Privacy / provenance note

These are **public** reference sources for testing — the concept's own reasoning
and material, not company artifacts. Private recordings for calibration stay
environment-local (ADR-0006) and are never committed.
