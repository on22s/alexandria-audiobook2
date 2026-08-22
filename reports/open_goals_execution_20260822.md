# Open-goals execution checkpoint — 2026-08-22

Scope: pursue every opportunity listed in the 2026-08-22 recommendation except
the LoRA dataset rebuild/source-ZIP recovery item, which the owner explicitly
excluded.

## Evidence already complete

- **Finished-audiobook preference (7.1):** the sealed eight-set package was
  rated on 2026-08-22. All three positive controls agreed; per-character
  instruction was byte-identical to no instruction; per-line instruction won
  no set; shipped casting beat scene-aware casting. Evidence:
  `blinded_listening_ratings.json`.
- **Long references / pitch (2.1, 2.5):** 150-line long-reference arms already
  exist for English, Japanese, and Chinese. English median-pitch preservation
  improved from 0.81 to 0.955, but ECAPA fell to 0.728 against a 0.833 human
  ceiling (87.4%). Chinese duration became 12.61x. A global longer-reference
  product change is rejected; it trades one real defect for worse identity and
  can trigger runaway duration.
- **Second English prosody set (2.9):** seven independent English LoRAs have
  20 held-out lines each. F0-correlation median is 0.447 (range 0.264–0.543),
  versus LJSpeech LoRA 0.289. LJSpeech contributes to the earlier deficit, but
  English remains below the CJK 0.72–0.74 range.
- **Expected prosody extraction (2.9):** Japanese and Chinese text-side
  extraction is complete on 150 lines each. These artifacts describe expected
  accent/tone; they do not yet fuse it with generated F0, so they are not the
  reference-free quality baseline the goal ultimately asks for.
- **Audio inspection (6.5):** four of ten registered views have owner ratings.
  The remaining six standalone views were copied to
  `~/Downloads/Alexandria Audio Inspection/` for inspection.
- **Unseen-book attribution (1.3):** the exact first-person narrator prior is
  already shipped and measured: 61.7% to 79.4% across three books. It closes a
  specific failure but not the broader 28-book gap. Three generic context or
  sequence prompt interventions failed their gates and must not be repeated.

## Remaining capability work

### Japanese preparation (5.4)

Reading-normalized CER is already met at 9.9%. Alignment alone remains open:
272 ms median against 150 ms. Silero VAD demonstrated 39 ms on its frozen
holdout, but it emits internal splits that overlap real inter-utterance gap
sizes. Threshold coalescing is unsafe. The next implementation must treat VAD
regions as ASR windows while retaining the parent utterance identity; it must
not assume one VAD region equals one output utterance.

### Conditional pronunciation (5.2 / 5.5)

The measured candidate table contains 1,056 rescues, but only 101 reproduced
in more than one arm. Of the terms in the active demo lexicon, only `kansai`
and `otsuka` have measured rescues, and each succeeded in only one of three
arms. Neither should ship without a blinded ear check. Blanket population is
rejected because respelling breaks about 70% of terms the plain reading already
says correctly.

### Reference-free prosody fusion (2.9)

Implement and validate the missing mapping from expected Japanese accent
phrases / Chinese tone sequences to time-aligned generated F0. Extraction alone
cannot score a generated audiobook. The first run must establish a baseline
before any threshold is invented.

### Generalisation (1.3)

The next pilot should add a persistent character representation rather than
another wording-only prompt. Published literary-attribution work reports gains
from sequential prediction and global character representations, particularly
on implicit/anaphoric quotations. A pilot must remain on the five-book open
pilot partition; the sealed twenty-book confirmation set stays sealed unless
the preregistered gate is met.

Primary research:

- https://aclanthology.org/2022.acl-long.400/
- https://aclanthology.org/2023.acl-short.64/
- https://aclanthology.org/2024.findings-emnlp.744/
