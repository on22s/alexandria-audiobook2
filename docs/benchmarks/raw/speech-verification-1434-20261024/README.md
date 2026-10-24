# #1434 finished-take verifier: false positives and missed defects, 334 cases

The production `verify_finished_speech` was run unchanged: Small.en primary, with a distinct Large-v3 confirmation when the primary finds something, on CPU. The harness is `app/experiments/speech_verification_faults.py`. `results.jsonl` holds one row per case: the verdict, both transcripts and seconds. Clip paths are reduced to file names.

## Cases

- **190 controls.** Real local-LoRA NARRATOR renders from `app/experiments/line_render_pilot.py`, each checked against the exact text its render proof says was sent. The proof establishes what was *requested*; nobody listened to confirm that each render actually spoke it, so a control is a render, not a known-clean take:
  - 80 Alice lines (#1545, 4 seeds);
  - 30 synthetic number/date lines (#1546);
  - 48 Alice/*Dorian Gray* passages (#1435, rendered as one request and as windows);
  - 16 short lines under 6 words (`v`) and 16 lines with authored repetition (`r`), seeds 14341–14342.

  All text is public domain or synthetic.
- **144 designed faults.** Exact PCM edits of one seed's controls, so the right answer is known by construction (`make_fault`):

  | fault | edit | lines |
  |---|---|---|
  | `tail_cut` | last 45% removed | 32 long lines |
  | `head_cut` | first 35% removed | 32 long lines |
  | `tail_repeat` | last 40% said twice | 32 long lines |
  | `mid_repeat` | the 40–60% span said twice in place | 32 long lines |
  | `short_double` | the whole short line said twice | 8 short lines |
  | `short_half` | the second half of a short line removed | 8 short lines |

Verdicts: `no_content_candidate` (clear), `review` (flagged for a listen), `corroborated_content_difference` (both ASR passes agree; the take is rejected).

## Results

**Controls: no control render was rejected.**

| control set | clear | review | rejected |
|---|---|---|---|
| Alice lines | 76 | 4 | 0 |
| number/date lines | 15 | **15** | 0 |
| passages, one request | 21 | 3 | 0 |
| passages, windows | 20 | 4 | 0 |
| short lines | 14 | 2 | 0 |
| authored repetition | 15 | 1 | 0 |
| **all** | **161** | **29 (15%)** | **0** |

- **What the 14 non-number reviews look like:** in the transcripts, all are spelling or normalization variants: Gryphon → griffin/griffon (4), "Never mind" → "Nevermind" (2), "Oh dear" → "O dear", dragon-fly/chirrup ("cheer up") and Hallward variants in two passages. That is what ASR wrote, not a listening verdict. Whether each render said the line correctly is **unresolved**, so these are not counted as confirmed false positives.
- **Number lines:** half are flagged, because ASR writes digits for spoken numbers. A book heavy with numbers will see a high review rate.
- **Authored repetition:** "No, no, no, no!" and "Off with her head! Off with her head!" were not flagged as faults (15 of 16 clear).

**Faults: long head/tail cuts are always noticed; repeats in the middle usually are not.**

| fault | missed (clear) | review | rejected |
|---|---|---|---|
| `tail_cut` | 0 | 11 | 21 |
| `head_cut` | 0 | 16 | 16 |
| `tail_repeat` | 3 | 12 | 17 |
| `mid_repeat` | **18 of 32** | 14 | 0 |
| `short_double` | 0 | 8 | 0 |
| `short_half` | 1 | 7 | 0 |

- **`mid_repeat` is the main gap.** In every missed case the primary transcript simply omits the second copy ("This time Alice waited patiently until it chose to speak again." for audio that says part of it twice). When the repeat is transcribed, it is flagged for review but never rejected.
  - This matches the earlier native observation on #1434 that Large-v3 collapses an exact duplicate.
  - These splices cut at arbitrary sample points, sometimes mid-word. A model's own repetition is usually whole words and may transcribe differently. That is not measured here.
- **Short lines:** doubling or halving a short line is always caught for review (15 of 16), never rejected automatically. This agrees with the earlier short-line hard-verdict note.

**Time (CPU, 3 workers alongside other work):**
- primary only: median 7.1 s (6.0–10.6) per take, 244 cases;
- with confirmation: median 51.1 s (43.5–64.1), 90 cases.

Peak memory was not measured here. The earlier #1434 qualification measured it.

## Limits

- One voice, English, Linux CPU, one machine.
- Designed faults are edits, not listened truth, and not a sample of real model failures.
- Rates come from this fixture set and are not a general accuracy figure.
- `results.jsonl` was produced before the harness bound each row to its input hash and verifier runtime. The verdict counts above are unaffected, but the current harness refuses to resume into these rows; a rerun needs a new `--out-dir`.
