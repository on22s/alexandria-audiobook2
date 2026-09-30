# A100 fold-swap attribution confirmation

Independent scoring of 5,543 sampled quotations per arm gives **63.03% base (3,494 correct) → 69.28% LoRA (3,840 correct)**: **+6.24 percentage points**, including unanswered rows as wrong. The adapter fixes 895 quotations and breaks 549. Six known positive/negative scorer controls passed before verifying all 11,086 records against the frozen answer keys; no correctness discrepancies were found.

The adapter was trained on 975 windows from nine books and evaluated on nineteen different books, with zero book overlap verified from the actual training rows. All 57 frozen evaluation files matched both their input manifest and independently fetched remote hashes. The source commit and harness fingerprint match the committed implementation. This is book-held-out evaluation; author independence has not been verified.

Serving used Qwen3-8B UD-Q2_K_XL with an F16 rank-16 LoRA, one llama.cpp server, full cast rosters, `michel2_full`, temperature zero, low reasoning, and at most 40 evenly distributed windows per book. Each window holds 25 segments. The launch sets a 1,024-token reasoning budget; evaluation starts at 4,096 output tokens with the existing 16,384-token retry ceiling. The log records 38 alternating adapter-scale confirmations (0.0/1.0). These are sampled attribution results, not full-book completion or audiobook listening scores.

Evaluation finished at **8:42 AM CDT on September 30, 2026**, after 11.81 hours of evaluation. No cloud job was changed to obtain or score these results.

| Book | Quotations per arm | Base | LoRA | Change (points) |
|---|---:|---:|---:|---:|
| ahandfulofdust | 494 | 59.51% | 64.17% | +4.66 |
| alicesadventuresinwonderland | 185 | 45.41% | 71.35% | +25.95 |
| anneofgreengables | 208 | 78.85% | 80.77% | +1.92 |
| apassagetoindia | 459 | 54.47% | 51.20% | -3.27 |
| aroomwithaview | 331 | 62.84% | 65.56% | +2.72 |
| daisymiller | 272 | 84.19% | 81.25% | -2.94 |
| hardtimes | 211 | 71.56% | 76.78% | +5.21 |
| howardsend | 382 | 67.54% | 74.35% | +6.81 |
| nightandday | 265 | 77.74% | 81.51% | +3.77 |
| olivertwist | 136 | 75.00% | 70.59% | -4.41 |
| theageofinnocence | 344 | 58.72% | 67.44% | +8.72 |
| thegambler | 246 | 51.22% | 53.66% | +2.44 |
| theinvisibleman | 234 | 59.40% | 70.09% | +10.68 |
| themanwhowasthursday | 110 | 68.18% | 74.55% | +6.36 |
| themysteriousaffairatstyles | 403 | 33.00% | 60.79% | +27.79 |
| thepictureofdoriangray | 344 | 76.45% | 84.59% | +8.14 |
| thesportofthegods | 325 | 69.23% | 74.46% | +5.23 |
| whereangelsfeartotread | 344 | 71.80% | 75.58% | +3.78 |
| winniethepooh | 250 | 55.20% | 57.60% | +2.40 |

The adapter improves 16 books and regresses on three. Giving every book equal weight yields +6.10 points; a seeded 20,000-draw bootstrap over books gives a 95% interval of +2.77 to +10.06 points. This describes uncertainty over these nineteen books, not every possible author or genre. The quote-level exact McNemar p-value is 7.34e-20, but quotations within a book are correlated; it should not replace the book-level assessment.

The adapter leaves **113 unanswered versus 89 for base**. Of 5,543 paired IDs, 5,354 received answers in both arms. Restricting to those gives 64.23% → 71.01%, +6.78 points, with 857 improvements and 494 regressions. That secondary figure excludes 189 IDs and is not the headline result.

**Interpretation:** the trained adapter improves attribution on books excluded from this training run, with losses on some books and more unanswered lines. This supports transfer for this frozen configuration. It does not close goal 1.3: the matching development comparison now measures a 3.27-point quote-weighted gap, documented in [the completed follow-up](thunder-japanese-development-2026-09-30.md); broader model-specific generalisation remains open. No adapter promotion or goal-target change is made.

**Replay limits:** raw responses and per-row prompt hashes are absent throughout this run. Verification reconstructs expected evaluation IDs from the frozen segmentation, exclusions, and window schedule, and recomputes the saved predictions against answer keys. It cannot independently reparse model replies or prove byte-identical prompts. The source, dataset, weights, converted adapter, server binary, and logs have recorded hashes.

Raw predictions, quotation text, corpus inputs, training examples, logs, and weights remain outside Git. No Chunagon material is involved.

- [Aggregate verification](../../ab_test_runtime/experiments/thunder_attribution_verification__20260930.json).
- [Replay verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_attribution.py).
- [Runtime and asset provenance](../../ab_test_runtime/evidence/thunder_results_20260930/a100_provenance.json).
