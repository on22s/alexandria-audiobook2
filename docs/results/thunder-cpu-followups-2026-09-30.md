# CPU follow-ups after the Thunder campaign

## Historical scoring coverage completed

The recovered private *The Sun Also Rises* answer key supplies the missing 345 rows per arm in the affected nine-book reports. All their 2,655 rows per arm are now rescored with the existing alias-aware scorer, with quotation text and expected labels checked against the fixtures. The corrections remain 25 base flags across three reports; the recovered fixture changes no additional flags. All 23 historical attribution summaries still match their saved arithmetic. Non-PDNC rows retain stored flags; each arm's actual PDNC coverage is in the aggregate. This does not validate old serving paths or training exclusions. The [historical comparison](thunder-historical-comparison-2026-09-30.md) retains the duplicate-report and boundary-audit findings.

## A100 saved-prediction error analysis

The input result hash matches the previously [verified held-out evaluation](thunder-a100-foldswap-2026-09-30.md). Every prediction's correctness was rechecked against its frozen answer key; all 19 fixture hashes match the evaluation metadata. All 5,543 paired IDs have identical saved candidate lists between arms. The seven accepting/rejecting controls include an answered wrong name with the correct speaker present in the roster, a missing-roster case, unanswered output and an alias-only roster match.

A **correct** row matches the answer key under the shared scorer. An **unanswered** row has an empty saved prediction. Other rows are classified by whether any saved candidate matches the gold speaker under those same aliases. An answered wrong prediction with a gold speaker in the roster is described as a selection error; this is an operational category, not a claim about the model's internal cause.

| Saved prediction category | Base | LoRA |
|---|---:|---:|
| Correct | 3,494 | 3,840 |
| Answered wrong, gold speaker in roster | 1,960 | 1,590 |
| Answered wrong, gold speaker absent | 0 | 0 |
| Unanswered | 89 | 113 |
| Total | 5,543 | 5,543 |

Alias-aware gold-speaker coverage is **5,543/5,543 in each arm**, including both correct and wrong rows. The evaluator uses full rosters augmented with gold-fixture cast names, so this is a supplied-roster attribution test, not a measurement of production roster discovery. Expanding these saved rosters cannot recover a missing gold name in this sample. Product roster discovery may still need separate work.

The **895 fixes** comprise 857 formerly answered-wrong rows and 38 formerly unanswered rows. The **549 regressions** comprise 494 answered-wrong rows and **55 newly unanswered** rows. LoRA's 113 blanks consist of 13 blanks retained from base, 55 formerly correct rows and 45 formerly answered-wrong rows. Of the base's 89 blanks, LoRA answers 38 correctly and 38 incorrectly, retaining 13. The net blank increase of 24 hides both gains and losses.

Even perfectly rescuing all 113 LoRA blanks leaves the 1,590 answered-wrong rows unresolved. This counterfactual is arithmetic, not a measured attainable intervention. Answered errors, unanswered errors and net adapter accuracy must stay separate.

| Held-out book | Rows | Fixes | Regressions | Regressions becoming unanswered | Base / LoRA unanswered |
|---|---:|---:|---:|---:|---:|
| ahandfulofdust | 494 | 84 | 61 | 5 | 2 / 6 |
| alicesadventuresinwonderland | 185 | 58 | 10 | 0 | 0 / 1 |
| anneofgreengables | 208 | 23 | 19 | 4 | 0 / 4 |
| apassagetoindia | 459 | 58 | 73 | 0 | 10 / 0 |
| aroomwithaview | 331 | 43 | 34 | 7 | 4 / 12 |
| daisymiller | 272 | 24 | 32 | 3 | 5 / 4 |
| hardtimes | 211 | 31 | 20 | 5 | 0 / 5 |
| howardsend | 382 | 64 | 38 | 1 | 0 / 1 |
| nightandday | 265 | 30 | 20 | 0 | 0 / 1 |
| olivertwist | 136 | 14 | 20 | 0 | 1 / 0 |
| theageofinnocence | 344 | 68 | 38 | 2 | 13 / 4 |
| thegambler | 246 | 27 | 21 | 13 | 26 / 40 |
| theinvisibleman | 234 | 37 | 12 | 1 | 0 / 4 |
| themanwhowasthursday | 110 | 17 | 10 | 1 | 0 / 2 |
| themysteriousaffairatstyles | 403 | 129 | 17 | 9 | 18 / 22 |
| thepictureofdoriangray | 344 | 52 | 24 | 0 | 0 / 0 |
| thesportofthegods | 325 | 50 | 33 | 0 | 2 / 0 |
| whereangelsfeartotread | 344 | 50 | 37 | 3 | 0 / 5 |
| winniethepooh | 250 | 36 | 30 | 1 | 8 / 2 |

The three books with net losses remain *A Passage to India* (58 fixes / 73 regressions), *Daisy Miller* (24 / 32) and *Oliver Twist* (14 / 20). Their regressions include zero, three and zero newly unanswered rows respectively. Thus blank handling alone would not explain or repair those losses. The Gambler contributes 13 of the 55 newly unanswered formerly-correct rows; it is a useful saved-example diagnostic set, not a reason to tune on held-out answers.

**Follow-up inference:** first reproduce answered selection failures and blank-producing windows on a development set, retaining controls for current correct answers. Candidate expansion is not supported by missing-name evidence in these saved samples. Raw responses and per-row prompt hashes are absent, so this CPU work cannot distinguish parsing failures, server failures, abstention or truncation behind blank predictions. It proposes no automatic routing policy and promotes no adapter.

## Goals reconciled

GOALS.md now records the completed matching development/held-out comparison in 1.3 (72.54% / 69.28%, 3.27-point gap for this frozen adapter), the paired duration evidence in 2.4, recognizer and boundary-alert limits in 5.4, and the completed English listening follow-ups in 7.1. It removes the stale request to repeat the matching comparison from the priority summary. Broader generalisation and listening goals remain open; narrow duration medians retain their existing status. Neither a three-times-duration runaway gate nor an ASR score closes a native-language or boundary-alignment goal.

[Portable CPU replay](../../ab_test_runtime/evidence/thunder_results_20260930/verify_cpu_followups.py) accepts two private report archives, the preserved private fixture, the saved A100 result and its fixture directory. [Aggregate verification](../../ab_test_runtime/experiments/thunder_cpu_followups_verification__20260930.json) records source, fixture, verifier and scorer hashes, counts, error transitions and per-book totals. No raw corpus text, predictions, transcripts, audio, private paths or credentials are published. No GPU inference, training or new human rating was performed.
