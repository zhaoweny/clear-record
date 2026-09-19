# Four-transmitter wireless capture rig (4 TX / 2 RX) — measured notes

Status: measured note from one kit test
Date: 2026-09-19
Provenance rule: every claim carries a label (**FACT / VOICE / REQ / DESIGN /
SUGGESTION / OPEN**). All measurements come from one take's files; the recording
and its transcripts stay environment-local (ADR-0006), and only aggregates appear
here.

The capture side of this project gets less attention than the models, so this note
records what a multi-transmitter wireless rig actually measures like when the
pipeline ingests it end to end — including the ways we fooled ourselves, because
the catch is the reusable part.

---

## 1. The rig

| Element | What it did |
|---|---|
| 4 × wireless transmitter | one per placement; each also records **internally** (48 kHz, 32-bit float, one file per 30 minutes) |
| 2 × receiver | quadraphonic over USB-C → 4 discrete channels; one receiver per host |
| laptop (macOS) | aggregate input: one receiver's 4 channels + the built-in microphone + a USB handheld recorder (7 channels) |
| desktop (Linux) | one receiver over USB; the pipeline host |
| phone | a narration memo for the take's notes |

**[FACT]** Placement was deliberately varied (a mousemat, a cart, a far table, and
near the neck) so the four tracks would differ rather than being four copies of one
placement. Seventeen sources survived compilation after dropping a byte-identical
duplicate memo.

---

## 2. What held up

**[FACT] Four discrete channels over USB work on Linux.** The receiver enumerates as
a single-alternate USB-audio device: `S24_3LE`, 4 channels, 48 kHz, 24-bit, channel
map `FL FR FC LFE`. It captures with plain ALSA or PipeWire and needs no vendor
driver. (The vendor's own compatibility list mentions macOS, Windows and iOS
software; the Linux path is unlisted but works. Note `arecord` may not be installed
— `ffmpeg -f alsa` and `pw-record` both do the job.)

**[FACT] The hosts' channel maps can be proven identical, not assumed.** Each host
captures a different receiver, but both receivers deliver the same digital audio for
a given transmitter, so the *corresponding* channels on the two hosts carry that
same audio; those pairs agree at r ≈ 0.99 over short windows (median ≈0.996 across
the take's 30 s windows, just over half of them above 0.99 — the figure moves with
the window and the position, because the relative clock lag slides; the recipe below
says how to read it). That is a stronger test than mapping a channel to a
*transmitter* by comparing it with that transmitter's internal recording: there the
two paths differ in processing, the correlation runs 0.34–0.75 on one host and
0.29–0.68 on the other, and one channel's best match changes with the window (its
top two candidates are 0.29 and 0.21). **Use the cross-host matrix for the channel
map; treat channel-to-transmitter assignment as an inference to confirm on the
receiver screen, not a measurement.**

**[FACT] One clock per host.** After alignment, every channel belonging to a host sat
within 64 ms of its siblings (60 ms across the PC host's channels, 64 ms across the
Mac's; each file is aligned independently, so read that as the estimate's own
scatter, not drift), and the four internal recordings fell within 47 ms of one
another over a 12-minute take.

**[FACT] Claps make a usable independent check on the alignment — at tens of
milliseconds, no finer.** A clap is impulsive, broadband and shared, so locating the
same clap in the fourteen other aligned captures cross-checks the estimated offsets
without using them. On this take the two isolated claps landed within −23…+55 ms of
the predicted positions, with mixed signs (17 of 28 residuals positive) — the same
size as the alignment's own per-file scatter (60 ms across the PC host's channels,
64 ms across the Mac's). It does *not* demonstrate a systematic bias: a reference-
side delay — the room microphone catching the clap through its reverb tail — would
push every residual negative, since each prediction is measured from the reference's
own clap, and these residuals are mixed.

**[FACT] Closeness to the mouth dominates every other placement effect we could
measure.** One ASR model on all sources, sorted by mean segment confidence; the
levels are 20 ms-frame statistics over 200–380 s of each transmitter's internal
recording, the confidence and transcript figures are the full take:

| placement | speech above own noise floor | mean segment confidence | transcript |
|---|---|---|---|
| at the neck | **30.5 dB** | **0.820** | 2,147 chars |
| by the mouse | 21.5 dB | 0.780 | 2,047 chars |
| on a cart | 16.8 dB | 0.776 | 1,919 chars |
| far table | 19.2 dB | 0.771 | 1,665 chars |
| (room microphone, for scale) | — | 0.810 | 2,382 chars |

**[FACT]** Noise floors sat within 2.7 dB of one another across all four transmitters
(−62.9…−65.6 dBFS), so the spread is signal, not noise. The two metrics agree
everywhere except that the cart and the far table swap — read it as "the near
microphone wins, the far ones are worse", not as a precise distance ladder.

**[OPEN]** Only *at the neck* is confirmed by the operator; the other three placement
labels follow from this ranking (the microphone-to-placement roster was not written
down before the transmitters went on), so those rows are inferred. (Lesson 4 below
exists because of this.)

---

## 3. The ways we fooled ourselves

**[FACT] 1. A correlation search too narrow to be right, read as a physical
finding.** Comparing the receivers' channels against the transmitters' internal
recordings gave r ≈ 0.01 for *every* pair, which "obviously" meant the two paths were
unrelated. It meant the files did not overlap in the window searched: the devices
were armed tens of seconds apart, and a ±2 s lag window cannot bridge that. *Caught
by:* noticing that in that same pass the four internal recordings agreed with each
other to within 31 ms, so the gap had to be between the transmitter group and the
other devices, not inside it. Two-stage alignment (coarse envelope correlation over
the whole files, then a fine waveform pass) found the real offsets — 32.6 s and
83.8 s — and the pairs then matched at the strengths §2 records (0.34–0.75 for
channel-to-transmitter, ≈0.99 across hosts).

**[FACT] 2. A units bug that made a coarse search look plausible.** The envelope
stage claimed 10 Hz but computed one value per **10 seconds**, so the coarse offsets
were quantised to ±10 s — outside the fine stage's ±0.5 s window, which then locked
onto whatever was nearby and looked reasonable. *Caught by:* printing the derived
durations: 7.4 s for a 12-minute file.

**[FACT] 3. Regressing one microphone against four others, and calling the
coefficients "distance".** Room microphone ≈ Σ gains × transmitters is tempting: a
nearer transmitter should have a bigger coefficient. But all four transmitters carry
the same speech, so the regressors are collinear and the coefficients are not
identifiable — re-derived over three different windows they swing from 0.22 to 1.54,
and left on *unaligned* files the same fit returns negative values, which is garbage
in, garbage out. *Caught by:* the spread across windows (a ranking that changes with
the window is not a ranking), and by re-deriving with measurements that cannot be
collinear — the per-source speech-to-floor range and the ASR output.

**[FACT] 4. Explaining an artifact with a mechanism nobody measured.** The neck-worn
transmitter appeared to correlate ~0 with the other transmitters, and our first pass
concluded its signal must be "dominated by clothing noise". Both halves were wrong.
The near-zero correlation came from the *unaligned* search of mistake 1; at the
aligned lag that transmitter sits in the top pair of transmitter-to-transmitter
correlations at every window (0.27–0.76, where the far pair reads 0.10–0.32). And it
is the *best* of the four on every measured axis, not the worst — loudest speech,
largest dynamic range, floors within 2.7 dB of the others. *Caught by:* an external
review demanding to know which of three very different claims "not useful" meant, and
by measuring instead of arguing. **Never explain a correlation you have not aligned
— and never treat one as a quality metric.**

**[FACT] 5. Matching transient peaks without a uniqueness constraint.** A first
clap-matching pass paired every peak within ±3 s of every other, which, with speech
transients in the mix, produced hundreds of meaningless pairs and sub-second medians
that were pure noise. *Caught by:* the medians contradicting a clean, independent
estimate. Match an isolated peak, one-to-one, in a tight window — then the check is
meaningful.

**[FACT] 6. Naming the alignment reference nowhere, and getting the default.** A
pipeline run that omits `--reference` silently takes the *first* source. Here the
first source was a narration memo that shares no content with the recording, and the
alignment resolved **3 of 17** sources; naming the room microphone resolved **15 of
17**. (The two that stay unresolved are the memos — correct, they have nothing to
correlate.) A related trap: a flag that *parsed* but was never forwarded to the stage
that would have used it, so the setting was ignored without a warning. Either way the
operator's lesson is the same: **name the reference explicitly in every command that
accepts one.**

**[FACT] 7. Scoring free-form commentary against a scripted reference.** Word-error
rate against the article text came out at 1.99 — because the recording contains the
commentary *and* the reading, while the reference contains only the reading.
Insertions swamp the metric. A take you intend to score has to be a clean read, or
the reference has to cover the whole recording.

---

## 4. [SUGGESTION] What we would tell the next person

1. **Arm in a written order, tear down in reverse**, so every file nests inside the
   longest one. Then the shared span is unambiguous before any correlation runs.
2. **Name the alignment reference** — a room microphone that hears everyone. It is
   also the presence gate for cross-talk attribution.
3. **Clap three times** (start, middle, end), the last one before the first stop.
4. **Write the roster down before the transmitters go on**: unit serial → transmitter
   number (assigned by pairing order, and it changes if you re-pair) → person →
   placement. Nothing downstream can recover it.
5. **Judge a transmitter by its own speech-to-floor range and its ASR output** — not
   by its level relative to others, not by its correlation with others.
6. **Print units and sanity-check magnitudes** before believing any statistic: three
   of the seven mistakes above would have died at that step.

---

## 5. [OPEN] Still open

- **Timecode.** The receivers can emit it (linear timecode (LTC) on a 3.5 mm output,
  or as audio on a channel), but the modes trade against the four-channel capture:
  audio-timecode occupies a channel, the LTC output mode degrades the four-channel
  layout, and "off" is what keeps the channels clean. A receiver can also act as a
  standalone timecode generator, so the cheap test is a second receiver feeding a
  spare recorder. Not yet run.
- **Per-source scoring on a clean read** — the experiment that would turn "which
  microphone is best" from a confidence proxy into a real word-error comparison.
- **Whether the receiver presents the same single 4-channel alternate setting in mono
  and 2-channel modes** — only the 4-channel mode was observed.

---

## 6. How to reproduce

**[FACT]** Alignment: 10 Hz RMS envelopes over the whole files, FFT cross-correlation
for a coarse lag, then a 60 s waveform cross-correlation refined to samples. Channel
identity: compare the two hosts' captures of the corresponding channel (one receiver
per host) with a per-window lag search over short windows (1–5 s) and read the median
— expect ≈ 0.99. The value is position-dependent: the two clocks slide relative to
each other across the take (≈24 ppm, in steps of up to ~5 ms per 30 s window), so an
individual window can read below 0.25 where a step falls inside it, while 3 s slices
inside such a window still read ≥ 0.8. Prefer short windows and report the median.
Clap check: find an isolated transient in the reference, then locate it in each other
file within a tight window around the prediction; read the residuals as a
tens-of-milliseconds check, not a millisecond one. Quality: 20 ms-frame levels
(10th/90th percentile for floor and speech) over a 180 s window of the internal
recording, plus the ASR segment confidence and transcript length over the full take.

**[FACT, repo]** Tooling: this project's pipeline for ingest/align/transcribe/attribute
(see the [README](../../README.md)), `whisper-cli` with small multilingual ggml models
for ASR, `ffmpeg` for decode and levels, NumPy for the analysis scripts.
