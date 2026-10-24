# Listening critic validation protocol (#1547)

Written before the listener round. It fixes the questions, the ground-truth rule and the decision procedure so the critic cannot be tuned to the listener after the fact.

## What is being tested

An audio-capable LLM critic that reports per-line and per-scene findings (`content`, `voice`, `style`, `pause`). The question is whether it finds the failures a listener hears, without inventing findings on clean takes. A critic that misses most failures cannot gate anything.

## Materials

- 30 lines and 5 scenes from *Alice's Adventures in Wonderland* (Lewis Carroll, 1865), public domain. The committed text is `app/fixtures/alice_pd_listening.json`; the clips are rendered locally and kept out of the repository.
- Lines cover 10 speakers, a range of instructs and lengths.
- Scenes contain 2-4 speakers with authored pauses, so voice consistency, pause fit and emotion fit are observable.
- 3 hidden repeats (one line with an instruct, one scene, one more line), inserted as ordinary trials with a minimum gap of 5 trials. Exact repeats may be recognisable; they are not labelled in the page.

## Blind round

- 38 trials (35 unique + 3 repeats), order fixed by seed 1547 and stored in a concealed key.
- The page shows the line text and its instruct, or the scene script. It never shows a critic output and never marks repeats.
- Scales are 1-5 with labels on the buttons:
  - line: naturalness, follows-its-instruct, says-the-line.
  - scene: same-voice-per-character, pause lengths, emotion matches the scene.
- The listener is the repository owner. Notes are free text. Ratings and notes are exported as JSON.

## Ground truth and scores

- A listener failure on a dimension is a rating of 1 or 2. Ratings of 3-5 are not failures.
- Critic findings map to dimensions: `content` -> says-the-line; `style` -> naturalness and follows-its-instruct; `voice` -> same-voice-per-character; `pause` -> pause lengths; emotion-fit is part of `style` for scenes.
- A critic is positive on a dimension when it reports at least one finding of the mapped type for that item. Severity is recorded but not used for the gate.
- Report precision and recall overall and per dimension, with bootstrap 95% intervals, plus repeat consistency for the listener (exact and within-one).

## Preregistered decision procedure

The critic passes this gate only if both hold:

1. the 95% bootstrap lower bound on overall recall is at least 0.50; and
2. the point estimate of overall precision is at least 0.50.

Additionally, if the listener marked at least 3 failures in any one dimension, that dimension's recall may not be zero. A critic that finds nothing in a whole category is not usable even if pooling hides it.

If the gate fails, stop: no production integration, record the null result. If it passes, findings may be shown as advisory only (retake or pause change), never as an automatic gate.

## Separate known-answer arm

The #1434 artifact (190 clean controls, 144 designed content faults) is a separate instrument check. It was scored before this protocol was written and is reported as evidence that the judge can hear content defects. It is not part of the decision above, because its faults are PCM edits, single-voice, and do not include voice, pause or style failures.

## Limits

- One listener, 35 unique items, one book and one voice set. This is an instrument check, not a general accuracy estimate.
- The designed faults are edits, not a sample of real model failures.
- Exact hidden repeats measure listener consistency; they do not make a noisy instrument precise.
- Only public-domain text and clips derived from it are sent to the API (Rule 27). Local audio and local-only measurements are not committed.

## Receipts

- Audio receipt (Gate A): `ab_test_runtime/listening_critic_1547/gate_a_receipt.json` (Qwen3.8 Omni Flash passed; Gemini 3.7 Flash rejected for fabricating on silence).
- Known-answer arm (Gate B0): `ab_test_runtime/listening_critic_1547/known_answers_receipt.json`.
- Listener responses and concealed key: local, named in the run notes below.

## Supplement added after the main listener round

The main round produced 8 failures, all on line naturalness or instruct fit, and none on content, scene voice, scene pause or scene emotion. Voice and pause recall were therefore unmeasurable from listener truth. A second blind round was added for those two dimensions, before the critic was run on either set.

- 12 trials: 3 clean scenes, 3 voice faults (one character's line pitch-shifted by +12% or -9% in place, preserving duration and text), 2 long-pause faults (3000 ms inserted at a boundary), 2 short-pause faults (a boundary pause set to 0), and 2 hidden repeats.
- The page shows the scene script and only the voice-consistency and pause-fit scales. Fault labels are known by construction and stored separately; the listener sees no label.
- This is a post-hoc supplement, not part of the original 30-line/5-scene design. It is reported as such. The main round's style evidence and the supplement's voice/pause evidence are kept separate.
- The preregistered decision procedure is unchanged. Content remains covered only by the separate #1434 known-answer arm.

## LoRA re-render (supersedes the custom-voice round for the gate)

The first main round used built-in custom voices because that run's workspace had no LoRA adapters. Its scenes therefore had little voice variation, and the voice dimension could not be tested as intended. The set was re-rendered with trained LoRA adapters and the listener round is repeated on that set. The earlier custom-voice ratings are retained as a secondary record, not as gate evidence.

- Cast: NARRATOR `husky_tenor_30s_m_literary`; ALICE `husky_soprano_20s_f`; HATTER `breathy_tenor_20s_m_fantasy`; QUEEN `crisp_mezzo_30s_f`; CHESHIRE CAT `silky_alto_40s_f_literary_1`; MOCK TURTLE `husky_baritone_50s_m_gothic`; DUCHESS `breathy_alto_50s_f_fantasy`; WHITE RABBIT `crisp_tenor_20s_m`; GRYPHON `gravelly_baritone_50s_m_scifi`; CATERPILLAR `husky_baritone_35s_m`; KING `breathy_baritone_40s_m_literary`; MARCH HARE `crisp_tenor_30s_m_fantasy`.
- Renders: 30 lines, 33 scene lines, and 3 adapter-swapped lines (HATTER line as KING, KING line as QUEEN, CATERPILLAR line as HATTER), seed 42, 66 clips, 5.8 minutes on the RX 9070 XT.
- Main page: seed 1551, 38 trials (35 + 3 hidden repeats), same scales and text/instruct display.
- Supplement: seed 1554, 12 trials. The three voice faults are now true LoRA adapter swaps, not pitch shifts; the two long-pause and two short-pause faults are rebuilt from the LoRA scenes; two hidden repeats.
- The decision procedure above is unchanged. The content arm remains the separate #1434 LoRA-narrator known-answer set.
- Limits: one listener, one book, one adapter per character, and the re-render is a second iteration after the custom-voice round. The LoRA round is the gate evidence; the custom round is reported only as prior evidence.

## Supplement blinding correction

The first supplement page displayed the label text as the scene title (`Clean scene 1`, `Voice change 1`, `Long pause 1`). That revealed the fault class and invalidates those listener ratings; they are discarded, not used as evidence. The replacement page keeps only the neutral chapter title, asks binary detection questions (voice changed yes/no; pause clearly too long/too short/none), and uses seed 1555 with two hidden repeats. The 1-5 supplement ratings are retained only as a record of the instrument failure, including the case where the listener's note identified the inserted pause but the 1-5 button still said 5.

The main page already used neutral chapter titles; it is unaffected.

## Results and decision (2026-10-24)

Listener round (LoRA voices, 38/38 trials):

- **8 instruct-fit failures** on 30 lines: line_05, 06, 08, 13, 16, 19, 20, 24.
- **0 naturalness, content, scene voice, scene pause or scene emotion failures.**
- Hidden repeats: 5/9 dimension pairs exact, 8/9 within one. `line_23` instruct fit was **2 then 5**, so that label is unreliable. `scene_03` pause fit was 4 then 3.
- The listener note on `scene_03` ("curiosity got cut off") describes a content defect, but scenes have no content scale, so it was not recorded as a failure.

Content known-answer arm (#1434, LoRA narrator clips): precision **0.972**, recall **0.958**; the same artifact's ASR baseline was **0.808 / 0.847**.

Critic on the same LoRA clips (Qwen3.8 Omni Flash, temperature 0), final run:

- Instruct fit: **precision 0.417, recall 0.625** (5/8), bootstrap recall lower bound **0.25**.
- Naturalness: 1 false positive on 30 clean lines; no listener positives.
- Content: 0 false positives on 30 clean lines.
- Designed voice faults: **1/3** detected, 3 false positives on 10 clips.
- Designed pause faults: **0/4** detected in the final run.

Two identical critic runs differed materially: instruct recall 3/8 vs 5/8; pause recall 2/4 vs 0/4; voice 1/3 in both. At temperature 0 this is provider/model instability, not sampling noise we control.

**Decision: the preregistered gate fails.** The 95% lower bound on listener instruct recall is 0.25 (required 0.50) and precision is 0.417 (required 0.50). Voice and pause listener truth was never obtained (the main round had zero positives; the detection round was not completed), and the known-answer voice/pause results are weak and unstable. Per the protocol, the critic is not integrated as a gate or as advisory findings. The result is recorded as a null result.

The content arm is the only strong signal, and it is content-only; it does not justify shipping a listening critic for voice, pause, style or instruct fit. Any future attempt needs a stable judge (repeated-run agreement) and a listener round that actually produces voice/pause failures before recall can be claimed.
