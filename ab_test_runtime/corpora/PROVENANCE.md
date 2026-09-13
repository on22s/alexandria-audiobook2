# Corpora used by the non-English attribution experiments

These files are **not** in version control — they are downloadable from the
sources below, and 11 MB of public-domain text does not belong in a repository
whose history is already several gigabytes. What is committed is this file, so
the data can be reconstructed.

They previously lived in a Claude session scratchpad whose path embedded a
session UUID, which meant `chinese_attribution.py`, `quote_aware_chunking.py`
and `japanese_quote_robustness.py` referenced a directory that would disappear
when that session was cleaned. Moved here on 2026-08-04.

Override the location with `ALEXANDRIA_CHINESE_CORPUS` or
`ALEXANDRIA_AOZORA_CORPUS`; otherwise the scripts look here.

## aozora/ — Japanese, public domain

Aozora Bunko (https://www.aozora.gr.jp/). Public domain in Japan; these authors
died more than 70 years ago.

| file | work | author |
| --- | --- | --- |
| `kokoro.txt` / `.html` | Kokoro | Natsume Sōseki |
| `ningen.txt` / `.html` | Ningen Shikkaku | Dazai Osamu |
| `rashomon.txt` / `.html` | Rashōmon | Akutagawa Ryūnosuke |

Used to test quote-aware chunking against 「」 and 『』 delimiters, which behave
differently from Western quotation marks.

## chinese/ — Chinese quotation attribution

| file | contents |
| --- | --- |
| `wp_train_instances.json`, `wp_dev_instances.json`, `wp_test.json` | World of Plainness (WP) splits |
| `wp_names.txt` | speaker roster |
| `jy_test.json` | Jin Yong (JY) test split |

From the Chinese speaker-identification datasets released under Apache-2.0 and
recorded in `THIRD_PARTY_NOTICES.md`. These are the corpora whose existence was
wrongly denied on 2026-08-03 before being found — see the note in that file.

## Reconstructing

Place the directories as `ab_test_runtime/corpora/aozora` and
`ab_test_runtime/corpora/chinese`. The experiments fail loudly with a missing
path rather than silently scoring nothing.

## hifitts/<reader>/ — English, a second public human-read reference set

Hi-Fi Multi-Speaker English TTS Dataset (Bakhturina, Lavrukhin, Ginsburg,
Zhang; Interspeech 2021; OpenSLR 109). LibriVox audio, Gutenberg text,
**CC BY 4.0**, 44.1 kHz, human transcripts (raw and normalised). Fetched per
reader from the parquet mirror `MikhailT/hifi-tts` by
`app/experiments/hifitts_fetch.py`, which reads each shard's `speaker`
column before deciding to download it, so one reader costs a few gigabytes
of transfer and never the 41 GB tarball. Written LJSpeech-shaped
(`wavs/<id>.wav`, `metadata.csv`, plus `corpus.json` naming the corpus,
licence and native rate) so `ljspeech_prepare.py` and everything after it
run unchanged; `id = <book_slug>-<chapter>_<seq>` so the split-by-source-work
rule holds.

| reader | name | sex | clean hours | works fetched (2026-09-13, ≤400 clips each) |
| --- | --- | --- | --- | --- |
| 9017 | John Van Stan | M | 58.0 | dartagnan01, dartagnan03part1, dartagnan03part3, zarathustra, antoinetteromances4, celebratedcrimesv1, historyofforestry |

Used by goal 2.9 (`run_chains/hifitts_9017_20260913.sh`) as the public
counterpart to the eight private narrators of `second_english_eval_20260820`.

## libriquote/<reader>/{quotes,narration}/ — one LibriVox reader's character speech beside their narration

LibriQuote (Michel, Epure, Cerisara; Findings of ACL 2026), test split. Every
character quotation is paired with the nearest narration utterance by the same
reader, both at 16 kHz under the dataset's `test_audios/`. Fetched per reader
by `app/experiments/libriquote_fetch.py`, which writes the two halves as
separate LJSpeech-shaped corpora (`wavs/<id>.wav`, `metadata.csv`,
`corpus.json`) with `id = <book>-<chapter>_<n>` so the split-by-source-work
rule holds. **CC BY-NC 4.0** — evidence only; nothing trained on it ships.

| reader | books (LibriVox ids) | used by |
| --- | --- | --- |
| 4992 | 3762 (Les Misérables vol. 5), 5957, 6056 | `run_chains/libriquote_4992_20260913.sh` — quotes-trained vs narration-trained adapter, cross-scored on the third book |

Reconstruct with `python app/experiments/libriquote_fetch.py --speaker 4992 --out ab_test_runtime/corpora/libriquote/4992`.
