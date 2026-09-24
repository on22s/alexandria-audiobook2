# Which model for your card

Every accuracy figure here is the **same measurement**: nine public-domain
novels, 2,655 speaker-labelled rows, the `michel2_full` prompt, batch 25,
request-level JSON schema, temperature 0, reasoning effort low, no adapter.
Nothing is extrapolated between rows, and nothing from a different fixture is
mixed in. Where a number does not exist yet the row says so rather than
guessing.

## Read this first: context length decides whether a model fits

File size is not the memory you need. The KV cache is, and it scales with the
context you ask for. Measured on the same 5.4 GB model:

| context | total VRAM |
|---|---:|
| `-c 4096` | **5.1 GB** |
| `-c 32768` | **11.3 GB** |

The same file needs twice its own size at 32k and barely more than its own size
at 4k. The product prompt's longest window measures **5,966 tokens**, so
**`-c 8192` is enough** — and choosing 8192 over 32768 is worth several
gigabytes, which is frequently the difference between fitting on your card and
not. Set it deliberately.

## The recommendations

| your card | run this | file | measured accuracy |
|---|---|---:|---:|
| **24 GB +** | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** |
| **16 GB** | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** |
| **12 GB** | Muse-Glimmer-30B **IQ3_XXS** | 10.6 GB | 92.6% |
| **10 GB** | Qwen3.8-27B **UD-IQ2_XXS** | 6.9 GB | 88.7% |
| **8 GB** | Qwen3.8-27B **UD-IQ2_XXS** | 6.9 GB | 88.7% |
| **6 GB** | *not yet measured* — cells queued | — | — |

### Three things that surprised us, all measured

**Do not pay for Q4.** Qwen3.8-27B at Q3_K_XL (12.5 GB) scored **95.2%** against
Q4_K_M (15.7 GB) at **94.9%**. Four gigabytes bought nothing; the difference is
inside the noise. The same holds for Muse: Q4_K_M 94.6% against Q3_K_XL 93.3%
for 3 GB more.

**A small quant of a big model beats a big quant of a small model.** Qwen3.8-27B
at IQ2_XXS is **6.9 GB and 88.7%**. Qwen3-14B at Q4_K_M is **9.0 GB and 84.3%**.
The smaller file of the larger model wins by 4.4 points. If you are choosing
between "a 14B I can run comfortably" and "a 27B I can just barely squeeze in",
squeeze in the 27B.

**The cliff is between Q3 and IQ2, not at Q4.** Qwen3.8 holds 95.2% at Q3_K_XL
and falls to 88.7% at IQ2_XXS — one step, 6.5 points. Everything above that step
is nearly flat. If you can reach 12.5 GB, reach it; below that, expect a real
cost rather than a gentle slope.

## The complete measured ladder

| model | quant | file | nine-novel accuracy |
|---|---|---:|---:|
| Qwen3.8-27B | UD-Q3_K_XL | 12.5 GB | **95.2%** |
| Qwen3.8-27B | UD-Q4_K_M | 15.7 GB | 94.9% |
| Muse-Glimmer-30B | KQuant Q4_K_M | 16.0 GB | 94.6% |
| Muse-Glimmer-30B | UD-Q3_K_XL | 12.75 GB | 93.3% |
| Muse-Glimmer-30B | IQ3_XXS | 10.6 GB | 92.6% |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | 22.4 GB | 92.1% |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | 12.6 GB | 91.6% |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | 10.3 GB | 90.5–91.6% |
| Qwen3.6-35B-A3B | UD-IQ1_M | 9.6 GB | 89.3% |
| Qwen3.8-27B | UD-IQ2_XXS | 6.9 GB | 88.7% |
| Qwen3-14B | Q4_K_M | 9.0 GB | 84.3% |

The A3B IQ2_XXS row is a range because two runs of the same cell on different
machines returned 91.6% and 90.5%. That 1.1-point spread is a useful scale for
reading every other row: differences smaller than about a point are not
differences.

## The prompt matters more than any of this

On the four-book fixture, switching the prompt from `default` to `michel2_full`
moved Qwen3.8-27B from 82.9% to 89.8% and Qwen3-14B from 65.9% to 82.0%. That is
larger than the gap between most adjacent rows above, and it costs nothing.

A concrete illustration of getting this wrong: a nine-novel panel run with the
`default` prompt, one entry per request, scored Qwen3-8B at **33.7%** — against
71.7% for the same model on the four-book fixture with `michel2_full` at batch
25. Three things differed at once, and the prompt was probably the largest. Pick
`michel2_full` (the shipped default) before you spend money on a bigger card.

## Not yet measured

- **6 GB cards.** Muse `Q1_0` (4.5 GB) and Qwen3.8 `Q1_L` (5.7 GB) are queued.
  Until those land there is no honest recommendation for this tier.
- **Muse below IQ3_XXS.** `IQ2_XXS` (7.4 GB) is queued.
- **Adapters.** Every row above is base-only. Adapters are measured separately
  and are only worth loading at the rungs where they are measured to help; see
  [Prompts and adapters](Prompts-and-Adapters).
